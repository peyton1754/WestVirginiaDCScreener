"""
Exercise the REAL patched query functions, lifted from their own source.

Neither `enrich_columns.py` nor `check_adjacent_land.py` can be imported in a
test environment: both pull in geopandas and both do work at module level. So
each function is located with `ast`, compiled on its own, and run against
stubbed requests and real-shaped ArcGIS responses.

That matters more here than usual. `tests/test_source_probe.py` proves the
classifier is right; this file proves the classifier is actually WIRED IN, and
that the callers unpack what the functions now return. Testing the helper alone
would have passed just as well with the scripts still swallowing every error,
which is the exact failure being fixed.
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import source_probe as pp  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch):
    """The lifted functions call source_probe.fetch without injecting a
    sleep, so the real backoff would run inside the suite: three attempts at
    1s then 2s, on every transport-failure fixture. The ATTEMPT COUNT is what
    these tests assert, and it is unaffected by how long the waits are."""
    monkeypatch.setattr(pp.time, 'sleep', lambda _s: None)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raises=None):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class FakeRequests:
    """Stands in for the `requests` module. Hands back a queued response, or
    raises a queued exception, and records the call."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def load_function(filename, func_name, extra_globals=None):
    """Compile one top-level function from a module without importing it."""
    src = open(os.path.join(REPO, filename), encoding='utf-8').read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            ns = {'source_probe': pp, 'HEADERS': {}, '__name__': 'lifted'}
            ns.update(extra_globals or {})
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         filename, 'exec'), ns)
            return ns[func_name], ns
    raise AssertionError(f"{func_name} not found at top level in {filename}")


SVC = {
    'url': 'https://services.wvgis.wvu.edu/arcgis/rest/services/'
           'Planning_Cadastre/WV_Parcels/MapServer/0/query',
    'owner': 'FullOwnerName', 'acres': 'Acres_C', 'landuse': None,
}

# The body WVGIS returns on the query shape this repo's own comment documents
# as failing: 400 "Failed to execute query."
WVGIS_400 = FakeResponse(400, {'error': {
    'code': 400, 'message': 'Failed to execute query.',
    'details': ['Failed to execute query.']}})

# An ArcGIS error delivered with HTTP 200, the case a status-code check misses.
ARCGIS_200_ERROR = FakeResponse(200, {'error': {
    'code': 400, 'message': 'Unable to complete operation.'}})

GOOD = FakeResponse(200, {'features': [
    {'attributes': {'FullOwnerName': 'ACME HOLDINGS LLC', 'Acres_C': 31.4}},
    {'attributes': {'FullOwnerName': 'SMALLER NEIGHBOR', 'Acres_C': 2.0}},
]})

TRULY_EMPTY = FakeResponse(200, {'features': []})


# --- enrich_columns.query_parcel_svc --------------------------------------

#: What the layer preflight established for this run. Injected because the
#: lifted function now defaults outFields to it rather than to the registry's
#: configured names, which is the whole point of the preflight.
PREFLIGHT_FIELDS = 'FullOwnerName,Acres_C'


def enrich_fn(outcome, out_fields=PREFLIGHT_FIELDS):
    req = FakeRequests(outcome)
    fn, _ns = load_function('enrich_columns.py', 'query_parcel_svc',
                            {'requests': req, 'PARCEL_OUT_FIELDS': out_fields})
    return fn, req


def test_enrich_returns_six_values_including_a_status_and_the_attempt_count():
    """The caller unpacks six. If this function still returned three, the
    caller would raise at runtime on the first candidate.

    `tries` joined the tuple when the retry landed. It is not decoration: the
    caller passes it to OutcomeCounter.record, which is the only thing that
    makes the retry's effect visible in a run log.
    """
    fn, _ = enrich_fn(GOOD)
    result = fn(SVC, -80.0, 39.0)
    assert len(result) == 6, (
        f"expected (owner, acres, imp, status, detail, tries), got {result!r}")
    assert result[5] == 1, "a clean first attempt must report one try"


def test_enrich_reads_a_real_parcel_and_picks_the_largest():
    fn, _ = enrich_fn(GOOD)
    owner, acres, _imp, status, _detail, _tries = fn(SVC, -80.0, 39.0)
    assert status == pp.OK
    assert owner == 'ACME HOLDINGS LLC'
    assert acres == pytest.approx(31.4)


def test_enrich_reports_a_genuine_empty_as_empty():
    fn, _ = enrich_fn(TRULY_EMPTY)
    owner, acres, _imp, status, _detail, _tries = fn(SVC, -80.0, 39.0)
    assert status == pp.EMPTY
    assert owner is None and acres is None


def test_enrich_reports_the_wvgis_400_as_a_failure_not_an_empty_site():
    """This is the Phase 1 case. Before the fix it returned (None, None, None),
    identical to a site with no parcel, and the run reported 0 matches."""
    fn, _ = enrich_fn(WVGIS_400)
    owner, acres, _imp, status, detail, tries = fn(SVC, -80.0, 39.0)
    assert status == pp.HTTP_ERROR, "an HTTP 400 must not read as 'no parcel here'"
    assert tries == 1, "a 400 is a wrong request; retrying it only takes longer to fail"
    assert status in pp.FAILURES
    assert '400' in detail
    assert owner is None and acres is None


def test_enrich_reports_an_arcgis_200_error_as_a_failure():
    fn, _ = enrich_fn(ARCGIS_200_ERROR)
    _o, _a, _i, status, detail, tries = fn(SVC, -80.0, 39.0)
    assert status == pp.SERVICE_ERROR
    assert 'Unable to complete operation' in detail
    assert tries == 1, (
        'the 2026-10-26 outage was this body on 25 queries; retrying it '
        'would have tripled the wall clock and reported the same nothing')


@pytest.mark.parametrize('exc', [
    TimeoutError('timed out'),
    ConnectionError('connection reset by peer'),
    OSError('TLS handshake failed'),
])
def test_enrich_reports_a_transport_failure_with_its_type(exc):
    fn, _ = enrich_fn(exc)
    _o, _a, _i, status, detail, tries = fn(SVC, -80.0, 39.0)
    assert status == pp.TRANSPORT_ERROR
    assert type(exc).__name__ in detail
    assert tries == 3, (
        'a transport failure is the class a retry recovers, so the real '
        'function must have attempted it more than once')


def test_enrich_still_sends_the_envelope_query_this_service_requires():
    """The repo's comment records that WVGIS errors on point+distance queries
    and only accepts an envelope. The fix must not have changed the query."""
    fn, req = enrich_fn(GOOD)
    fn(SVC, -80.0, 39.0)
    assert len(req.calls) == 1
    _url, kw = req.calls[0]
    params = kw['params']
    assert params['geometryType'] == 'esriGeometryEnvelope'
    assert params['returnGeometry'] == 'false'
    assert params['f'] == 'json'
    assert len(params['geometry'].split(',')) == 4


def test_enrich_files_a_malformed_feature_as_bad_json_not_a_network_failure():
    """The service answered; it was the SHAPE that was wrong. The parsing used
    to sit inside the request's try, so a feature missing "attributes" reached
    classify_exception and the run printed
    `transport_error: KeyError: 'attributes'`, telling the reader the network
    had failed when it had not."""
    malformed = FakeResponse(200, {'features': [{'no_attributes_key': 1}]})
    fn, _ = enrich_fn(malformed)
    _o, _a, _i, status, detail, _tries = fn(SVC, -80.0, 39.0)
    assert status == pp.BAD_JSON, f"got {status}, which blames the wrong layer"
    assert status in pp.FAILURES
    assert 'attributes' in detail


def test_enrich_files_a_non_numeric_acreage_as_bad_json():
    weird = FakeResponse(200, {'features': [
        {'attributes': {'FullOwnerName': 'ACME', 'Acres_C': 'not a number'}}]})
    fn, _ = enrich_fn(weird)
    _o, _a, _i, status, _d, _t = fn(SVC, -80.0, 39.0)
    assert status == pp.BAD_JSON


def test_enrich_asks_only_for_fields_the_service_has():
    fn, req = enrich_fn(GOOD)
    fn(SVC, -80.0, 39.0)
    out_fields = req.calls[0][1]['params']['outFields']
    assert 'Shape__Area' not in out_fields, "the comment says this field errors the service"
    assert 'FullOwnerName' in out_fields and 'Acres_C' in out_fields


# --- check_adjacent_land.query_adjacent -----------------------------------

def adjacent_fn(outcome):
    req = FakeRequests(outcome)
    counter = pp.OutcomeCounter('test')
    fn, _ns = load_function('check_adjacent_land.py', 'query_adjacent',
                            {'requests': req, 'ADJACENT_OUTCOMES': counter})
    return fn, req, counter


def test_adjacent_returns_the_status_alongside_the_features():
    """It used to return a bare list, so the caller got [] for a failure and []
    for a genuine empty and reported both as 'no parcels within 600m', however
    carefully this function tallied them. The status has to leave the function."""
    fn, _req, counter = adjacent_fn(GOOD)
    status, feats = fn(SVC, -80.0, 39.0)
    assert status == pp.OK
    assert len(feats) == 2
    assert counter.counts[pp.OK] == 1
    assert counter.failed == 0


def test_adjacent_hands_back_a_failure_status_the_caller_can_branch_on():
    fn, _req, counter = adjacent_fn(WVGIS_400)
    status, feats = fn(SVC, -80.0, 39.0)
    assert status == pp.HTTP_ERROR
    assert status in pp.FAILURES
    assert feats == []
    assert counter.failed == 1, "the failure must be tallied, not silently dropped"
    assert counter.counts[pp.EMPTY] == 0, "a failure must not count as a genuine empty"


def test_adjacent_counts_a_genuine_empty_as_empty():
    fn, _req, counter = adjacent_fn(TRULY_EMPTY)
    status, feats = fn(SVC, -80.0, 39.0)
    assert status == pp.EMPTY
    assert status not in pp.FAILURES
    assert feats == []
    assert counter.counts[pp.EMPTY] == 1
    assert counter.failed == 0


def test_adjacent_records_a_transport_failure_rather_than_raising():
    fn, _req, counter = adjacent_fn(TimeoutError('timed out'))
    status, feats = fn(SVC, -80.0, 39.0)
    assert status == pp.TRANSPORT_ERROR
    assert feats == []
    assert counter.counts[pp.TRANSPORT_ERROR] == 1


# --- the callers were updated too, not just the functions -----------------

def test_the_enrich_caller_unpacks_the_status():
    """A grep-level guard on the call sites. If someone restores a three-value
    unpack the function's status is silently discarded again."""
    src = open(os.path.join(REPO, 'enrich_columns.py'), encoding='utf-8').read()
    assert 'owner, acres, imp, status, detail, tries = query_parcel_svc(' in src
    assert 'owner, acres, imp = query_parcel_svc(' not in src, (
        "a caller still unpacks three values and drops the status")
    assert 'owner, acres, imp, status, detail = query_parcel_svc(' not in src, (
        "a caller unpacks five and drops the attempt count, so the retry's "
        "effect never reaches the run log")


def test_the_enrich_caller_reports_its_outcomes():
    src = open(os.path.join(REPO, 'enrich_columns.py'), encoding='utf-8').read()
    assert 'statewide_outcomes.record(' in src
    assert 'statewide_outcomes.report()' in src, (
        "outcomes are tallied but never printed, so the run log still cannot "
        "distinguish a failure from an empty result")


def counter_attribute_uses(filename, counter_name):
    """Every attribute accessed on a named counter, found by parsing the module.

    A substring search would match the name inside a comment or a docstring,
    which is exactly how a write-only counter went unnoticed.
    """
    tree = ast.parse(open(os.path.join(REPO, filename), encoding='utf-8').read())
    return {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == counter_name}


def test_the_adjacent_counter_is_read_and_not_only_written():
    """A counter that is recorded into and never read produces no output at
    all, however correct its tallies are. That shipped once: the comment above
    its definition said it was 'reported at the end of the run' while the only
    attribute ever used on it was .record, so the whole apparatus was
    write-only and a run where every query failed printed no summary."""
    uses = counter_attribute_uses('check_adjacent_land.py', 'ADJACENT_OUTCOMES')
    assert 'record' in uses, "nothing records into the counter"
    assert uses & {'report', 'lines'}, (
        "ADJACENT_OUTCOMES is written but never read, so its tallies reach "
        f"nobody. Attributes used: {sorted(uses)}")


def summarize_with(query_result, own_acres=30.0):
    """Lift summarize_adjacent with its collaborators stubbed.

    Behavioural, not a substring search. The first version of this test
    asserted `'query_failed' in src`, which a mutation defeated trivially: it
    changed the returned status back to 'no_features' while the string
    survived elsewhere in the file, and the test stayed green. A source-text
    assertion checks that a word is present, not that the code does anything
    with it.
    """
    fn, _ns = load_function(
        'check_adjacent_land.py', 'summarize_adjacent',
        {'STATEWIDE_SVC': SVC, 'COUNTY_OVERRIDES': {},
         'query_adjacent': lambda svc, lon, lat, buffer_m=600: query_result,
         'is_likely_vacant': lambda owner, landuse: False,
         'identify_site_parcel': lambda parcels, own: (0, 'stubbed'),
         'VACANCY_BASIS': 'owner_name_only',
         # Lifted from the real source, not stubbed. A stub here would have
         # hidden the NaN defect that finite_acres exists to close.
         'finite_acres': load_function('check_adjacent_land.py',
                                       'finite_acres')[0]})
    # own_acres is the single base every acreage figure is measured from. It
    # used to be absent, and the caller added it for the CSV while
    # expansion_verdict() added the largest parcel in the buffer instead.
    return fn('Test Site', 'HARRISON', 39.0, -80.0, own_acres)


def test_a_failed_adjacent_query_is_not_reported_as_no_parcels():
    """The service never answered, so nothing is known about this site's
    neighbours. Reporting that as 'no parcels within 600m' states a finding
    that was never measured."""
    result = summarize_with((pp.HTTP_ERROR, []))
    assert result['status'] != 'no_features', (
        "a failed query is still reported as a site with no adjacent parcels")
    assert result['status'] == 'query_failed'
    assert result.get('query_status') == pp.HTTP_ERROR
    assert 'not a finding' in result['note'].lower() or 'FAILED' in result['note']


def test_a_genuine_empty_adjacent_query_is_still_reported_as_no_parcels():
    """The other direction. The service answered, so 'no parcels within 600m'
    is a real finding and must still be reported as one."""
    result = summarize_with((pp.EMPTY, []))
    assert result['status'] == 'no_features'
    assert 'FAILED' not in result['note']


def test_the_two_adjacent_outcomes_do_not_share_a_status():
    failed = summarize_with((pp.TRANSPORT_ERROR, []))
    empty = summarize_with((pp.EMPTY, []))
    assert failed['status'] != empty['status'], (
        "a failed query and a genuine empty still carry the same status, which "
        "is the defect this change exists to fix")


def test_the_adjacent_caller_keeps_the_status_query_adjacent_returns():
    src = open(os.path.join(REPO, 'check_adjacent_land.py'), encoding='utf-8').read()
    assert 'status, feats = query_adjacent(' in src, (
        "the caller discards the status query_adjacent now returns")


def test_a_failed_adjacent_query_is_not_written_to_the_csv_as_a_zero():
    """A zero in vacant_adjacent_acres is a measurement: it says this site has
    no vacant land beside it. Writing one for a query that never answered puts
    a fabricated measurement in the CSV, and anyone summing or sorting that
    column treats an outage as zero vacancy."""
    src = open(os.path.join(REPO, 'check_adjacent_land.py'), encoding='utf-8').read()
    for col in ('vacant_adjacent_acres', 'same_owner_adjacent_acres'):
        line = next(l for l in src.splitlines() if f'"{col}":' in l)
        assert '_failed' in line, f"{col} still writes a number when the query failed: {line.strip()}"


def total_potential_fn():
    fn, _ns = load_function('check_adjacent_land.py', 'total_potential_acres')
    return fn


def test_a_failed_query_has_no_total_potential():
    """Behavioural, not a grep. total_potential_acres() is now the only place
    this sum is computed, so the guarantee belongs to the function."""
    fn = total_potential_fn()
    assert fn({'status': 'query_failed', 'query_status': pp.HTTP_ERROR}) is None


def test_a_genuine_empty_still_has_a_total():
    """The service answered that nothing is adjacent. The total is the site's
    own acreage, and returning None there would lose a real figure."""
    fn = total_potential_fn()
    assert fn({'status': 'no_features', 'base_acres': 30.0,
               'vacant_acres': 0.0, 'same_owner_acres': 0.0}) == 30.0


def test_the_total_is_the_base_plus_both_adjacent_figures():
    fn = total_potential_fn()
    assert fn({'status': 'ok', 'base_acres': 28.0,
               'vacant_acres': 137.8, 'same_owner_acres': 0.0}) == pytest.approx(165.8)


def test_the_total_is_none_when_the_base_is_missing():
    """A sum built on a missing base would silently read as the adjacent
    acreage alone, which is a smaller number that looks like a measurement."""
    fn = total_potential_fn()
    assert fn({'status': 'ok', 'base_acres': None,
               'vacant_acres': 137.8, 'same_owner_acres': 0.0}) is None


def test_the_verdict_and_the_csv_read_the_same_function():
    """THE REGRESSION TEST for the measured defect. Row 5 of the shipped
    adjacent_land_25_50ac.csv carried total_potential_acres 165.81604589 next
    to a verdict reading "STRONG - up to 301 acres possible". The CSV added
    the candidate row's acreage; the verdict added the largest parcel in the
    600m box, which for that site is a 163-acre neighbour."""
    src = open(os.path.join(REPO, 'check_adjacent_land.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    callers = {n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == 'total_potential_acres'}
    assert len(callers) >= 2, (
        f"total_potential_acres is called from {len(callers)} place(s). The "
        f"verdict and the CSV cell must both read it, or they can disagree "
        f"again")
    assert 'result["site_acres"] + vacant + same' not in src, (
        "the verdict is computing its own total again")


def test_the_parcel_query_defaults_to_what_the_preflight_established():
    """Not to the registry's configured names. Sending a field the layer does
    not have is the documented cause of this service's 'Failed to execute
    query', which is what took out all 25 queries on 2026-10-26."""
    fn, req = enrich_fn(GOOD, out_fields='ONLY_WHAT_THE_LAYER_HAS')
    fn(SVC, -80.0, 39.0)
    assert req.calls[0][1]['params']['outFields'] == 'ONLY_WHAT_THE_LAYER_HAS'


def test_the_parcel_query_can_ask_for_every_field():
    """The fallback when layer metadata is unavailable. outFields=* cannot
    name a field the layer lacks."""
    fn, req = enrich_fn(GOOD, out_fields='*')
    fn(SVC, -80.0, 39.0)
    assert req.calls[0][1]['params']['outFields'] == '*'


def test_attributes_are_read_case_insensitively():
    """With outFields=* the attribute keys are whatever the layer calls them,
    and case-insensitive field resolution means the key may differ in case
    from the registry's spelling. An exact match would silently read None and
    report the site as having no parcel, which is the bug class being fixed."""
    differently_cased = FakeResponse(200, {'features': [
        {'attributes': {'FULLOWNERNAME': 'ACME HOLDINGS LLC', 'acres_c': 31.4}}]})
    fn, _ = enrich_fn(differently_cased)
    owner, acres, _imp, status, _d, _t = fn(SVC, -80.0, 39.0)
    assert status == pp.OK
    assert owner == 'ACME HOLDINGS LLC', "owner lost to a case difference"
    assert acres == pytest.approx(31.4), "acreage lost to a case difference"


def test_the_two_formerly_swallowed_sources_now_tally_and_report():
    """water_basin and soil_drainage used the same
    `if r.status_code == 200: ... except Exception: pass` shape as the parcel
    query, so a failed service read as an unfilled row."""
    for counter in ('wbd_outcomes', 'soil_outcomes'):
        uses = counter_attribute_uses('enrich_columns.py', counter)
        assert 'record' in uses, f"{counter} is never recorded into"
        assert uses & {'report', 'lines'}, (
            f"{counter} is write-only, so its tallies reach nobody")


def test_every_classified_response_is_actually_recorded():
    """The guard that would have caught all three outages at once.

    Checking only that a counter is recorded into SOMEWHERE is too coarse:
    each source has a second record() call on its parse-error path, so
    deleting the main one left the name in the file and the test green. A
    mutation sweep caught that. This instead pairs each classify_* call with
    a record() of its result, which is the thing that must never be dropped.
    """
    src = open(os.path.join(REPO, 'enrich_columns.py'), encoding='utf-8').read()
    lines = src.splitlines()
    tree = ast.parse(src)

    # `fetch` counts as a classification site: it calls classify_response
    # and classify_exception itself, so a status it returns is exactly as
    # droppable as one classified inline. A test that knew only about
    # classify_* went to zero findings the moment the call sites moved to
    # fetch, and read that as a clean file.
    HANDLERS = ('classify_', 'fetch')
    classified = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and any(call.func.attr.startswith(h) for h in HANDLERS)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == 'source_probe'):
            # end_lineno, not lineno. A fetch() call is a dozen lines of
            # params, so a window measured from the FIRST line ends inside
            # the call's own arguments and never sees the record() below it.
            classified.append((node.lineno, node.end_lineno))

    assert len(classified) >= 3, (
        f"expected a classified or fetched response per source (water_basin, "
        f"soil, parcel), found {len(classified)}")

    # A classified status is handled if it is either recorded into a counter
    # here, or RETURNED to a caller that records it. query_parcel_svc does the
    # latter, and requiring record() at the call site would have failed it
    # wrongly. What must never happen is a status that goes nowhere at all.
    unhandled = []
    for lineno, end_lineno in classified:
        window = '\n'.join(lines[end_lineno - 1: end_lineno + 8])
        recorded = '.record(' in window
        returned = any(l.strip().startswith('return') and 'status' in l
                       for l in window.splitlines())
        if not (recorded or returned):
            unhandled.append(lineno)
    assert unhandled == [], (
        f"a response is classified but its status is neither recorded nor "
        f"returned, at lines {unhandled}. The classification reaches nobody, "
        f"which is the same as swallowing it.")


def test_no_request_in_the_enrichment_file_still_swallows_its_errors():
    """Structural guard. Two handlers of the form `except Exception: pass`
    around a live request are what hid three separate service outages. This
    fails if one comes back, rather than waiting for another silent run."""
    src = open(os.path.join(REPO, 'enrich_columns.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    swallowed = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.ExceptHandler)
                 and len(n.body) == 1 and isinstance(n.body[0], ast.Pass)]
    assert swallowed == [], f"silent exception handlers at lines {swallowed}"

    # `if r.status_code == 200:` with no else is the other half of the idiom:
    # it drops every non-200 without a word.
    offenders = [i for i, line in enumerate(src.splitlines(), 1)
                 if 'status_code' in line and '200' in line]
    assert offenders == [], (
        f"the status-code swallow idiom is back at lines {offenders}; "
        f"use source_probe.classify_response instead")


def test_a_failed_enrich_query_leaves_a_trace_on_disk():
    """Without this the status lives only as long as the terminal scrollback:
    a failed row and a genuinely parcel-less row are byte-identical in
    top_candidates_wv.csv, both with a blank owner and no parcel_acres."""
    src = open(os.path.join(REPO, 'enrich_columns.py'), encoding='utf-8').read()
    assert 'QUERY_FAILED:' in src, (
        "a failed parcel query writes nothing to any column, so the "
        "distinction does not survive the run")
    assert 'source_probe.FAILURES' in src


# --- the retry must be IN the pipeline, not merely available --------------
# A mutation sweep on 2026-10-26 found four vacuous spots at once: changing
# `attempts=3` to `attempts=1` at the water_basin, soil and adjacency call
# sites, and dropping `tries` from a counter's record(), all left the suite
# green. source_probe's own tests prove the retry WORKS; only these prove it
# is wired into the scripts, which is the same gap that let three services
# swallow their errors for weeks while the helper beneath them was correct.

RETRY_SITES = [
    ('enrich_columns.py', 'WBD_URL'),      # water_basin: the measured failures
    ('enrich_columns.py', 'SSURGO_URL'),   # soil
    ('enrich_columns.py', "svc[\"url\"]"),  # the parcel query
    ('check_adjacent_land.py', "svc[\"url\"]"),
]


def fetch_calls(filename):
    """Every `source_probe.fetch(...)` call in a module, as AST nodes."""
    src = open(os.path.join(REPO, filename), encoding='utf-8').read()
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'fetch'
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'source_probe'):
            out.append((node, ast.get_source_segment(src, node) or ''))
    return out


@pytest.mark.parametrize('filename,url_marker', RETRY_SITES)
def test_every_fetch_site_actually_retries(filename, url_marker):
    """`attempts=1` is not a retry. It is the pre-fix behaviour with a new
    function name in front of it."""
    matching = [(n, seg) for n, seg in fetch_calls(filename) if url_marker in seg]
    assert matching, (
        f"no source_probe.fetch call in {filename} mentions {url_marker}. "
        f"Either the call site moved or it no longer goes through fetch at all")
    for node, seg in matching:
        attempts = next((kw.value for kw in node.keywords if kw.arg == 'attempts'), None)
        assert attempts is not None, (
            f"{filename}:{node.lineno} fetches without naming `attempts`, so "
            f"the retry count is whatever the default happens to be")
        assert isinstance(attempts, ast.Constant) and attempts.value >= 2, (
            f"{filename}:{node.lineno} passes attempts={getattr(attempts, 'value', '?')}, "
            f"which performs no retry")


def test_the_positive_control_for_the_marker_search():
    """If a marker matched nothing the test above would fail loudly, but a
    marker that matches the WRONG call would pass quietly. This pins the
    count: four fetch sites across the two files, no more and no fewer."""
    total = len(fetch_calls('enrich_columns.py')) + len(fetch_calls('check_adjacent_land.py'))
    assert total == 4, (
        f"expected 4 fetch call sites (water_basin, soil, parcel, adjacency), "
        f"found {total}. A new one needs a row in RETRY_SITES or it is "
        f"unguarded; a lost one means a source went back to a bare request")


COUNTER_SITES = [
    ('enrich_columns.py', 'wbd_outcomes'),
    ('enrich_columns.py', 'soil_outcomes'),
    ('enrich_columns.py', 'statewide_outcomes'),
    ('enrich_columns.py', 'county_outcomes'),
    ('check_adjacent_land.py', 'ADJACENT_OUTCOMES'),
]


@pytest.mark.parametrize('filename,counter', COUNTER_SITES)
def test_the_attempt_count_reaches_the_counter(filename, counter):
    """A retry whose effect is never reported is an assumption. The counter's
    recovery line is the only place a run says what the retry bought, and it
    needs `tries` to say it.

    The parse-error paths deliberately call record() with two arguments: the
    initial record already counted the retry, and calling it again with
    `tries` would double-count. So this asserts that AT LEAST ONE record()
    on each counter carries the attempt count, not that every one does.
    """
    src = open(os.path.join(REPO, filename), encoding='utf-8').read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == 'record' and isinstance(n.func.value, ast.Name)
             and n.func.value.id == counter]
    assert calls, f"nothing records into {counter} in {filename}"
    with_tries = [n for n in calls
                  if len(n.args) >= 3
                  or any(kw.arg == 'tries' for kw in n.keywords)]
    assert with_tries, (
        f"every {counter}.record() in {filename} drops the attempt count, so "
        f"the run log cannot say whether any query recovered on a retry")
