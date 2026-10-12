"""
Tests for parcel_probe.

The fixtures are built from the shapes this service actually returns, not from
shapes invented to pass. The two that matter, and that the old code could not
see, are an HTTP 400 and an ArcGIS error body served with HTTP 200. Both
previously reached `r.json().get("features", [])` and came out as an empty
list, indistinguishable from a site with no parcel.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import parcel_probe as pp  # noqa: E402


class FakeResponse:
    """Stands in for a requests.Response. `json()` raises like the real one
    when the body will not parse."""

    def __init__(self, status_code=200, payload=None, raises=None):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


# --- positive controls ----------------------------------------------------
# If these fail, every "is a failure" assertion below means nothing.

def test_a_real_result_is_ok():
    r = FakeResponse(200, {'features': [{'attributes': {'Acres_C': 12.5}}]})
    status, detail, feats = pp.classify_response(r)
    assert status == pp.OK
    assert len(feats) == 1
    assert '1 parcel' in detail


def test_a_genuine_empty_is_empty_not_a_failure():
    """The service answered, correctly, that there is nothing here. This is
    the only status that licenses the claim 'this site has no parcel'."""
    status, _detail, feats = pp.classify_response(FakeResponse(200, {'features': []}))
    assert status == pp.EMPTY
    assert status not in pp.FAILURES
    assert feats == []


# --- the two cases that made the Phase 1 failure invisible ----------------

def test_http_400_is_a_failure_not_an_empty_result():
    """requests does not raise on 4xx, so the old code parsed the error body,
    found no 'features' key, and returned an empty list."""
    r = FakeResponse(400, {'error': {'code': 400, 'message': 'Failed to execute query.'}})
    status, detail, feats = pp.classify_response(r)
    assert status == pp.HTTP_ERROR, "an HTTP 400 must never read as 'no parcels here'"
    assert status in pp.FAILURES
    assert '400' in detail
    assert feats == []


def test_arcgis_error_served_with_http_200_is_a_failure():
    """ArcGIS returns many real errors as HTTP 200 with an error object, which
    is why a status-code check alone is not enough."""
    r = FakeResponse(200, {'error': {
        'code': 400,
        'message': 'Unable to complete operation.',
        'details': ["Invalid or missing input parameters."]}})
    status, detail, feats = pp.classify_response(r)
    assert status == pp.SERVICE_ERROR
    assert status in pp.FAILURES
    assert 'Unable to complete operation' in detail
    assert feats == []


@pytest.mark.parametrize('code', [401, 403, 404, 429, 500, 502, 503])
def test_every_error_status_code_is_a_failure(code):
    status, _d, _f = pp.classify_response(FakeResponse(code, {'features': []}))
    assert status in pp.FAILURES, f"HTTP {code} read as a non-failure"


def test_an_html_body_is_a_failure_not_an_empty_result():
    """A proxy block page or a gateway error is HTML. json() raises, and the
    old bare except turned that into 'no parcel here'."""
    r = FakeResponse(200, raises=ValueError('Expecting value: line 1 column 1'))
    status, detail, feats = pp.classify_response(r)
    assert status == pp.BAD_JSON
    assert 'not JSON' in detail
    assert feats == []


def test_a_body_with_neither_features_nor_error_is_a_failure():
    """An unrecognised shape is not evidence of absence."""
    status, detail, feats = pp.classify_response(FakeResponse(200, {'count': 0}))
    assert status == pp.BAD_JSON
    assert 'features' in detail
    assert feats == []


def test_features_of_the_wrong_type_is_a_failure():
    status, _d, _f = pp.classify_response(FakeResponse(200, {'features': {'a': 1}}))
    assert status == pp.BAD_JSON


def test_a_json_array_body_is_a_failure():
    status, _d, _f = pp.classify_response(FakeResponse(200, [1, 2, 3]))
    assert status == pp.BAD_JSON


def test_a_transport_exception_is_a_failure_carrying_its_type():
    status, detail, feats = pp.classify_exception(TimeoutError('timed out after 15s'))
    assert status == pp.TRANSPORT_ERROR
    assert 'TimeoutError' in detail and 'timed out' in detail
    assert feats == []


def test_no_failure_status_ever_returns_features():
    """A caller that ignores the status must still be unable to mistake a
    failure for data."""
    cases = [
        FakeResponse(400, {'features': [{'attributes': {}}]}),
        FakeResponse(200, {'error': {'message': 'x'}, 'features': [{'attributes': {}}]}),
        FakeResponse(200, raises=ValueError('nope')),
    ]
    for r in cases:
        status, _d, feats = pp.classify_response(r)
        assert status in pp.FAILURES
        assert feats == [], "a failed query handed back features"


# --- the counter ----------------------------------------------------------

def test_counter_separates_answered_from_failed():
    c = pp.OutcomeCounter('WVGIS')
    c.record(pp.OK, '3 parcels')
    c.record(pp.EMPTY, 'zero parcels')
    c.record(pp.HTTP_ERROR, 'HTTP 400')
    assert c.total == 3
    assert c.answered == 2
    assert c.failed == 1
    assert not c.all_failed()


def test_counter_says_so_in_words_when_everything_failed():
    """The Phase 1 case. A zero that means 'the service never answered' must
    not be reported the same way as a zero that means 'no parcels exist'."""
    c = pp.OutcomeCounter('WVGIS')
    for _ in range(26):
        c.record(pp.HTTP_ERROR, 'HTTP 400: Failed to execute query.')
    text = '\n'.join(c.lines())
    assert c.all_failed()
    assert '26 FAILED' in text
    assert 'not of the sites' in text, "the warning must say what the zero is a measurement OF"
    assert 'HTTP 400: Failed to execute query.' in text, "the first failure detail must survive"


def test_counter_is_quiet_when_nothing_failed():
    c = pp.OutcomeCounter('WVGIS')
    for _ in range(10):
        c.record(pp.OK, '1 parcel')
    text = '\n'.join(c.lines())
    # The summary line always ends "0 FAILED", so match the bracketed markers
    # rather than the bare word.
    assert '[FAIL]' not in text and '[WARN]' not in text
    assert '10 with parcels' in text
    assert '0 FAILED' in text


def test_counter_warns_when_failures_match_or_outnumber_answers():
    c = pp.OutcomeCounter('WVGIS')
    c.record(pp.OK); c.record(pp.EMPTY)
    c.record(pp.HTTP_ERROR, 'HTTP 500'); c.record(pp.TRANSPORT_ERROR, 'TimeoutError: x')
    text = '\n'.join(c.lines())
    assert 'understate coverage' in text


def test_counter_keeps_the_first_detail_per_status_not_the_last():
    c = pp.OutcomeCounter()
    c.record(pp.HTTP_ERROR, 'first failure')
    c.record(pp.HTTP_ERROR, 'later failure')
    text = '\n'.join(c.lines())
    assert 'first failure' in text and 'later failure' not in text


def test_counter_rejects_an_unknown_status():
    """A typo'd status must not be silently dropped from the tally, which is
    the same class of defect this module exists to fix."""
    with pytest.raises(ValueError):
        pp.OutcomeCounter().record('definitely_not_a_status')


def test_an_empty_counter_prints_nothing_alarming():
    c = pp.OutcomeCounter('WVGIS')
    text = '\n'.join(c.lines())
    assert '0 queries' in text
    assert 'WARN' not in text, "no queries is not the same as every query failing"


def test_report_uses_the_given_printer():
    """Asserts CONTENT, not self-equality.

    This previously asserted only `out == c.lines()`, the renderer compared
    against itself, which passes even when lines() returns nothing at all. A
    cold review caught it: stubbing lines() to return [] failed five other
    tests and left this one green.
    """
    c = pp.OutcomeCounter('WVGIS')
    c.record(pp.OK)
    c.record(pp.HTTP_ERROR, 'HTTP 400')
    out = []
    c.report(printer=out.append)
    assert out, "report() printed nothing"
    text = '\n'.join(out)
    assert 'WVGIS' in text and '2 queries' in text
    assert '[FAIL] http_error: 1' in text
    assert 'HTTP 400' in text


def test_a_response_without_a_status_code_is_not_given_a_free_pass():
    """A requests.Response always carries status_code, so an object without one
    means some other client is in play and the transport was never checked.
    Classifying it as a clean empty would skip the HTTP check entirely."""
    class NoStatusCode:
        def json(self):
            return {'features': []}

    status, detail, feats = pp.classify_response(NoStatusCode())
    assert status == pp.BAD_JSON, "the HTTP check was silently skipped"
    assert 'status_code' in detail
    assert feats == []


def test_first_detail_keeps_the_first_NON_EMPTY_example():
    """record() defaults detail to '', and a caller that omits it must not
    claim the slot and leave the summary with a failure count and no
    explanation. Pinned because the docstring and the code disagreed about
    this until a cold review measured it."""
    c = pp.OutcomeCounter()
    c.record(pp.HTTP_ERROR, '')
    c.record(pp.HTTP_ERROR, 'the first real explanation')
    c.record(pp.HTTP_ERROR, 'a later one')
    assert c.first_detail['http_error'] == 'the first real explanation'
    assert c.counts[pp.HTTP_ERROR] == 3, "every occurrence still counts"
