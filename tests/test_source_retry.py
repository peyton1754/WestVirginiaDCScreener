"""
Tests for source_probe's retry layer.

WHAT MEASURED THIS INTO EXISTENCE
---------------------------------
Refresh run 5 (id 40378766799, 2026-10-26) filled water_basin for 23 of 31
sites. The 8 misses were not absences: 5 were HTTP 504 and 3 were ReadTimeout
against hydro.nationalmap.gov. Those are the two shapes a second attempt
recovers, and nothing in the pipeline attempted one.

The tests that matter most here are the NEGATIVE ones. A retry that fires on
the wrong failure is worse than no retry: the 2026-10-26 parcel outage was an
ArcGIS error body served with HTTP 200, and retrying it would have turned a
25-query failure into a 75-query failure that took three times as long to
report the same nothing.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import source_probe as pp  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raises=None):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


def scripted(*outcomes):
    """A `call` that walks a script of responses and exceptions.

    Also records how many times it was invoked, so a test can assert the
    number of ATTEMPTS independently of the `tries` the function reports.
    Trusting the returned count alone would let a function that lies about
    its own attempts pass.
    """
    calls = []

    def call():
        calls.append(len(calls) + 1)
        item = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    call.calls = calls
    return call


def no_sleep(_seconds):
    pass


# --- positive controls ----------------------------------------------------
# If a clean first attempt does not come back OK on one call, every
# attempt-count assertion below is measuring the wrong thing.

def test_a_first_attempt_success_is_not_retried():
    call = scripted(FakeResponse(200, {'features': [{'attributes': {}}]}))
    status, _detail, rows, tries = pp.fetch(call, sleep=no_sleep)
    assert status == pp.OK
    assert tries == 1
    assert len(call.calls) == 1
    assert len(rows) == 1


def test_a_genuine_empty_is_not_retried():
    """The service answered. Asking again cannot make a site have a parcel,
    and an EMPTY is the one status that licenses an absence claim."""
    call = scripted(FakeResponse(200, {'features': []}))
    status, _d, _r, tries = pp.fetch(call, sleep=no_sleep)
    assert status == pp.EMPTY
    assert tries == 1
    assert len(call.calls) == 1


# --- the measured failures: 504 and ReadTimeout ---------------------------

def test_a_504_then_success_recovers_and_reports_two_tries():
    call = scripted(FakeResponse(504),
                    FakeResponse(200, {'features': [{'attributes': {}}]}))
    status, _d, rows, tries = pp.fetch(call, sleep=no_sleep)
    assert status == pp.OK
    assert tries == 2
    assert len(call.calls) == 2
    assert len(rows) == 1


def test_a_read_timeout_then_success_recovers():
    call = scripted(TimeoutError('read timed out'),
                    FakeResponse(200, {'features': [{'attributes': {}}]}))
    status, _d, _r, tries = pp.fetch(call, sleep=no_sleep)
    assert status == pp.OK
    assert tries == 2


def test_a_persistent_504_exhausts_and_says_so():
    call = scripted(FakeResponse(504))
    status, detail, rows, tries = pp.fetch(call, attempts=3, sleep=no_sleep)
    assert status == pp.HTTP_ERROR
    assert tries == 3
    assert len(call.calls) == 3
    assert 'after 3 attempts' in detail
    assert 'HTTP 504' in detail
    # A failure never hands back rows, however many attempts it took.
    assert rows == []


# --- the negative controls, which are the point ---------------------------

def test_an_arcgis_error_body_is_never_retried():
    """The 2026-10-26 parcel outage. HTTP 200 carrying {"error": ...} is a
    REJECTED QUERY. Retrying it triples the wall clock and reports the same
    nothing, and it was the whole reason that outage was invisible."""
    call = scripted(FakeResponse(200, {'error': {'code': 400,
                                                 'message': 'Failed to execute query.'}}))
    status, detail, _r, tries = pp.fetch(call, attempts=3, sleep=no_sleep)
    assert status == pp.SERVICE_ERROR
    assert tries == 1
    assert len(call.calls) == 1
    assert 'after 3 attempts' not in detail


def test_a_400_is_never_retried():
    call = scripted(FakeResponse(400))
    status, _d, _r, tries = pp.fetch(call, attempts=3, sleep=no_sleep)
    assert status == pp.HTTP_ERROR
    assert tries == 1
    assert len(call.calls) == 1


def test_a_403_is_never_retried():
    """A refusal to serve this caller at all. Asking again changes nothing."""
    call = scripted(FakeResponse(403))
    _s, _d, _r, tries = pp.fetch(call, attempts=4, sleep=no_sleep)
    assert tries == 1


def test_an_html_error_page_is_never_retried():
    """A proxy block page parses as BAD_JSON. It is a configuration answer,
    not a transient one."""
    call = scripted(FakeResponse(200, raises=ValueError('Expecting value')))
    status, _d, _r, tries = pp.fetch(call, attempts=3, sleep=no_sleep)
    assert status == pp.BAD_JSON
    assert tries == 1


def test_a_429_is_retried():
    call = scripted(FakeResponse(429), FakeResponse(200, {'features': [{'attributes': {}}]}))
    status, _d, _r, tries = pp.fetch(call, sleep=no_sleep)
    assert status == pp.OK
    assert tries == 2


@pytest.mark.parametrize('code', [500, 502, 503, 504, 408, 425, 429])
def test_every_retryable_code_is_retryable(code):
    assert pp.is_retryable(pp.HTTP_ERROR, FakeResponse(code)) is True


@pytest.mark.parametrize('code', [400, 401, 403, 404, 405, 410, 422, 200])
def test_no_other_code_is_retryable(code):
    assert pp.is_retryable(pp.HTTP_ERROR, FakeResponse(code)) is False


def test_a_service_error_is_not_retryable_whatever_the_response_carries():
    assert pp.is_retryable(pp.SERVICE_ERROR, FakeResponse(200)) is False
    assert pp.is_retryable(pp.SERVICE_ERROR, FakeResponse(504)) is False


def test_a_transport_error_is_retryable_with_no_response_at_all():
    """Nothing came back, so there is no status code to consult."""
    assert pp.is_retryable(pp.TRANSPORT_ERROR) is True
    assert pp.is_retryable(pp.TRANSPORT_ERROR, None) is True


def test_a_response_with_no_status_code_is_not_retried():
    """classify_response refuses a shape it cannot vouch for. is_retryable
    must not read a missing attribute as a retryable one."""
    class Weird:
        def json(self):
            return {'features': []}

    assert pp.is_retryable(pp.HTTP_ERROR, Weird()) is False
    call = scripted(Weird())
    status, _d, _r, tries = pp.fetch(call, attempts=3, sleep=no_sleep)
    assert status == pp.BAD_JSON
    assert tries == 1


# --- edges ----------------------------------------------------------------

def test_attempts_of_one_disables_the_retry_without_claiming_a_retry():
    call = scripted(FakeResponse(504))
    status, detail, _r, tries = pp.fetch(call, attempts=1, sleep=no_sleep)
    assert status == pp.HTTP_ERROR
    assert tries == 1
    assert len(call.calls) == 1
    # "(after 1 attempts)" is both ungrammatical and a false claim.
    assert 'attempts)' not in detail


def test_attempts_below_one_is_refused_rather_than_silently_skipping_the_call():
    """Returning a failure for attempts=0 would report the SERVICE as broken
    when the caller is. Zero queries measures nothing."""
    call = scripted(FakeResponse(200, {'features': []}))
    with pytest.raises(ValueError):
        pp.fetch(call, attempts=0, sleep=no_sleep)
    assert call.calls == []


def test_the_backoff_doubles_and_does_not_sleep_after_the_last_attempt():
    slept = []
    call = scripted(FakeResponse(503))
    pp.fetch(call, attempts=3, delay=1.0, sleep=slept.append)
    assert slept == [1.0, 2.0]


def test_the_backoff_scales_with_the_delay():
    slept = []
    pp.fetch(scripted(FakeResponse(503)), attempts=4, delay=0.5, sleep=slept.append)
    assert slept == [0.5, 1.0, 2.0]


def test_a_success_sleeps_not_at_all():
    slept = []
    pp.fetch(scripted(FakeResponse(200, {'features': [{'attributes': {}}]})),
             sleep=slept.append)
    assert slept == []


def test_the_data_key_reaches_the_classifier():
    """SSURGO returns rows under "Table". A retry layer that hardcoded
    "features" would turn every soil answer into a BAD_JSON and then not
    retry it, which reads as the soil service being broken."""
    call = scripted(FakeResponse(200, {'Table': [['row']]}))
    status, _d, rows, _t = pp.fetch(call, data_key='Table', sleep=no_sleep)
    assert status == pp.OK
    assert rows == [['row']]


def test_a_failure_that_changes_shape_between_attempts_reports_the_last_one():
    """504 then a rejected query. The second answer is the informative one and
    is also not retryable, so it ends the loop."""
    call = scripted(FakeResponse(504),
                    FakeResponse(200, {'error': {'code': 400, 'message': 'nope'}}))
    status, detail, _r, tries = pp.fetch(call, attempts=3, sleep=no_sleep)
    assert status == pp.SERVICE_ERROR
    assert 'nope' in detail
    assert tries == 2


# --- the counter's recovery tally ----------------------------------------

def test_the_counter_reports_what_the_retry_bought():
    c = pp.OutcomeCounter('test service')
    c.record(pp.OK, '1 row(s)', tries=1)
    c.record(pp.OK, '1 row(s)', tries=2)
    c.record(pp.HTTP_ERROR, 'HTTP 504 (after 3 attempts)', tries=3)
    assert c.retried == 2
    assert c.recovered == 1
    assert any('1 of 2 retried' in line for line in c.lines())


def test_a_recovered_empty_counts_as_recovered():
    """The service answering "nothing here" on the second attempt is a
    recovery: before the retry that site read as a failure."""
    c = pp.OutcomeCounter('test service')
    c.record(pp.EMPTY, 'service returned zero rows', tries=2)
    assert c.recovered == 1


def test_no_retries_prints_no_recovery_line():
    c = pp.OutcomeCounter('test service')
    c.record(pp.OK, '1 row(s)')
    assert not any('recovered' in line for line in c.lines())


def test_tries_below_one_is_refused():
    c = pp.OutcomeCounter('test service')
    with pytest.raises(ValueError):
        c.record(pp.OK, '', tries=0)


def test_record_still_takes_two_arguments():
    """Every existing call site passes (status, detail). Adding `tries` must
    not force them all to change."""
    c = pp.OutcomeCounter('test service')
    c.record(pp.OK, '1 row(s)')
    c.record(pp.EMPTY)
    assert c.total == 2
    assert c.retried == 0
