"""
Exercise the REAL patched query functions, lifted from their own source.

Neither `enrich_columns.py` nor `check_adjacent_land.py` can be imported in a
test environment: both pull in geopandas and both do work at module level. So
each function is located with `ast`, compiled on its own, and run against
stubbed requests and real-shaped ArcGIS responses.

That matters more here than usual. `tests/test_parcel_probe.py` proves the
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

import parcel_probe as pp  # noqa: E402


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
            ns = {'parcel_probe': pp, 'HEADERS': {}, '__name__': 'lifted'}
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

def enrich_fn(outcome):
    req = FakeRequests(outcome)
    fn, _ns = load_function('enrich_columns.py', 'query_parcel_svc',
                            {'requests': req})
    return fn, req


def test_enrich_returns_five_values_including_a_status():
    """The caller unpacks five. If this function still returned three, the
    caller would raise at runtime on the first candidate."""
    fn, _ = enrich_fn(GOOD)
    result = fn(SVC, -80.0, 39.0)
    assert len(result) == 5, f"expected (owner, acres, imp, status, detail), got {result!r}"


def test_enrich_reads_a_real_parcel_and_picks_the_largest():
    fn, _ = enrich_fn(GOOD)
    owner, acres, _imp, status, _detail = fn(SVC, -80.0, 39.0)
    assert status == pp.OK
    assert owner == 'ACME HOLDINGS LLC'
    assert acres == pytest.approx(31.4)


def test_enrich_reports_a_genuine_empty_as_empty():
    fn, _ = enrich_fn(TRULY_EMPTY)
    owner, acres, _imp, status, _detail = fn(SVC, -80.0, 39.0)
    assert status == pp.EMPTY
    assert owner is None and acres is None


def test_enrich_reports_the_wvgis_400_as_a_failure_not_an_empty_site():
    """This is the Phase 1 case. Before the fix it returned (None, None, None),
    identical to a site with no parcel, and the run reported 0 matches."""
    fn, _ = enrich_fn(WVGIS_400)
    owner, acres, _imp, status, detail = fn(SVC, -80.0, 39.0)
    assert status == pp.HTTP_ERROR, "an HTTP 400 must not read as 'no parcel here'"
    assert status in pp.FAILURES
    assert '400' in detail
    assert owner is None and acres is None


def test_enrich_reports_an_arcgis_200_error_as_a_failure():
    fn, _ = enrich_fn(ARCGIS_200_ERROR)
    _o, _a, _i, status, detail = fn(SVC, -80.0, 39.0)
    assert status == pp.SERVICE_ERROR
    assert 'Unable to complete operation' in detail


@pytest.mark.parametrize('exc', [
    TimeoutError('timed out'),
    ConnectionError('connection reset by peer'),
    OSError('TLS handshake failed'),
])
def test_enrich_reports_a_transport_failure_with_its_type(exc):
    fn, _ = enrich_fn(exc)
    _o, _a, _i, status, detail = fn(SVC, -80.0, 39.0)
    assert status == pp.TRANSPORT_ERROR
    assert type(exc).__name__ in detail


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
    _o, _a, _i, status, detail = fn(SVC, -80.0, 39.0)
    assert status == pp.BAD_JSON, f"got {status}, which blames the wrong layer"
    assert status in pp.FAILURES
    assert 'attributes' in detail


def test_enrich_files_a_non_numeric_acreage_as_bad_json():
    weird = FakeResponse(200, {'features': [
        {'attributes': {'FullOwnerName': 'ACME', 'Acres_C': 'not a number'}}]})
    fn, _ = enrich_fn(weird)
    _o, _a, _i, status, _d = fn(SVC, -80.0, 39.0)
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
    assert 'owner, acres, imp, status, detail = query_parcel_svc(' in src
    assert 'owner, acres, imp = query_parcel_svc(' not in src, (
        "a caller still unpacks three values and drops the status")


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


def summarize_with(query_result):
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
         'is_likely_vacant': lambda owner, landuse: False})
    return fn('Test Site', 'HARRISON', 39.0, -80.0)


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
    for col in ('vacant_adjacent_acres', 'same_owner_adjacent_acres',
                'total_potential_acres'):
        line = next(l for l in src.splitlines() if f'"{col}":' in l)
        assert '_failed' in line, f"{col} still writes a number when the query failed: {line.strip()}"


def test_a_failed_enrich_query_leaves_a_trace_on_disk():
    """Without this the status lives only as long as the terminal scrollback:
    a failed row and a genuinely parcel-less row are byte-identical in
    top_candidates_wv.csv, both with a blank owner and no parcel_acres."""
    src = open(os.path.join(REPO, 'enrich_columns.py'), encoding='utf-8').read()
    assert 'QUERY_FAILED:' in src, (
        "a failed parcel query writes nothing to any column, so the "
        "distinction does not survive the run")
    assert 'parcel_probe.FAILURES' in src
