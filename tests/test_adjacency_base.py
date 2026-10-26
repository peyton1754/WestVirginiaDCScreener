"""
One base for the expansion figure, and an honest vacancy basis.

THE MEASURED DEFECT
-------------------
Row 5 of the shipped outputs/csv/adjacent_land_25_50ac.csv:

    site_acres              28.01604589
    vacant_adjacent_acres   137.8
    same_owner_adjacent...  0.0
    total_potential_acres   165.81604589
    expansion_verdict       STRONG - up to 301 acres possible

166 and 301, in the same row, and neither cell said which base it used. The
CSV added the candidate row's own acreage; expansion_verdict() added
result["site_acres"], which was the LARGEST PARCEL IN THE 600m BOX. For
AURORA ACQUISITION those differ by 135 acres, because the largest parcel in
the box is a 163-acre neighbour and not the site at all.

Three linked defects came out of that one assumption:

(a) Two bases, two numbers, one row.
(b) "largest in the buffer is the site" is wrong whenever a neighbour is
    bigger, which was true in one of the two rows on file.
(c) When it is wrong, the site's own parcel stays inside `neighbors`, so the
    vacant and same-owner acreage can include the site itself.

The worst available case is already in top_candidates_wv.csv: DTE SMITH
BRANCH carries parcel_acres of 23,475.98, owner BLUESTONE RESOURCES INC, a
coal landholding returned because a point query hands back whatever polygon
the point lands in. Treat one of those as "the site" and a 28-acre candidate
reads as STRONG expansion potential.

THE VACANCY BASIS
-----------------
wv.json sets parcels.statewide_service.landuse to null, because the WVGIS
layer has no land-use field: refresh run 4 (id 40276191635) printed all 21 of
its field names. So is_likely_vacant() matches OWNER NAMES only on WV, and a
parcel counts as vacant because its owner is called something containing
"FARM" or "TIMBER". That may be the best signal available. It is not a
land-use classification, and nothing in the output said so.
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import source_probe as pp  # noqa: E402


#: Pure helpers lifted into every namespace, because the functions under
#: test call them and `load_function` compiles one function at a time. They
#: are lifted from the REAL source rather than stubbed: a stub for
#: finite_acres would have hidden the NaN defect this file found.
ALWAYS_LIFT = ('finite_acres',)


def load_function(filename, func_name, extra_globals=None, with_helpers=True):
    """Compile one top-level function without importing the module.

    check_adjacent_land.py reads a CSV and runs queries at module level, so
    it cannot be imported in a test environment.
    """
    src = open(os.path.join(REPO, filename), encoding='utf-8').read()
    tree = ast.parse(src)
    wanted = {func_name}
    if with_helpers:
        wanted |= {h for h in ALWAYS_LIFT if h != func_name}

    ns = {'source_probe': pp, 'HEADERS': {}, '__name__': 'lifted'}
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            found[node.name] = node
    missing = wanted - set(found)
    assert not missing, (
        f"{sorted(missing)} not found at top level in {filename}")

    # Helpers first, then any caller-supplied override, so a test can still
    # stub a collaborator deliberately.
    for name in list(ALWAYS_LIFT) + [func_name]:
        if name in found:
            exec(compile(ast.Module(body=[found[name]], type_ignores=[]),
                         filename, 'exec'), ns)
    ns.update(extra_globals or {})
    return ns[func_name]


def identify(parcels, own_acres):
    fn = load_function('check_adjacent_land.py', 'identify_site_parcel',
                       {'SITE_MATCH_FRACTION': 0.5,
                        'SITE_MATCH_FLOOR_ACRES': 10.0})
    return fn([{'owner': o, 'acres': a, 'landuse': ''} for o, a in parcels],
              own_acres)


# --- the AURORA case, with its real numbers -------------------------------

AURORA_PARCELS = [
    ('A 163 ACRE NEIGHBOUR', 163.2),
    ('FRANZ CHARLES BRADLEY', 28.01604589),
    ('A SMALL NEIGHBOUR', 2.0),
]


def test_the_site_parcel_is_matched_by_acreage_not_by_being_largest():
    """The measured case. AURORA's own parcel is 28.016 acres and the largest
    in its 600m box is a 163-acre neighbour."""
    idx, note = identify(AURORA_PARCELS, 28.01604589)
    assert idx == 1, (
        f"picked index {idx}, the {AURORA_PARCELS[idx][1]}-acre parcel. The "
        f"site's own parcel is 28.016 acres")
    assert '28.0' in note


def test_a_coal_landholding_is_not_mistaken_for_the_site():
    """The DTE SMITH BRANCH shape: a 23,476-acre parcel in the buffer and a
    28-acre site. Matching rather than guessing is what keeps a 28-acre
    candidate from reading as STRONG."""
    idx, note = identify([('BLUESTONE RESOURCES INC', 23475.98)], 28.0)
    assert idx is None, "a 23,476-acre parcel was accepted as a 28-acre site"
    assert '23475' in note or '23476' in note, (
        f"the refusal does not say what it rejected: {note}")


def test_an_unmatched_site_excludes_nothing_rather_than_guessing(monkeypatch):
    """With no site parcel identified, every returned parcel counts as a
    neighbour. That overstates adjacency, which is the cheaper error: it is
    flagged in the verdict, whereas a wrong match silently credits the site
    with a neighbour's acreage."""
    fn = load_function(
        'check_adjacent_land.py', 'summarize_adjacent',
        {'STATEWIDE_SVC': {'owner': 'FullOwnerName', 'acres': 'CALC_ACRE',
                           'landuse': None},
         'COUNTY_OVERRIDES': {},
         'query_adjacent': lambda svc, lon, lat, buffer_m=600: (
             pp.OK, [{'attributes': {'FullOwnerName': 'BLUESTONE RESOURCES INC',
                                     'CALC_ACRE': 23475.98}}]),
         'is_likely_vacant': lambda owner, landuse: False,
         'identify_site_parcel': load_function(
             'check_adjacent_land.py', 'identify_site_parcel',
             {'SITE_MATCH_FRACTION': 0.5, 'SITE_MATCH_FLOOR_ACRES': 10.0}),
         'VACANCY_BASIS': 'owner_name_only'})
    result = fn('DTE SMITH BRANCH', 'WYOMING', 37.6, -81.5, 28.0)
    assert result['site_parcel_identified'] is False
    assert result['n_neighbors'] == 1, (
        "the unmatched parcel was dropped from the neighbour set, so it was "
        "treated as the site after all")
    assert result['base_acres'] == 28.0, (
        "the base moved off the candidate row's acreage")


# --- the tolerance -------------------------------------------------------

def test_an_osm_derived_acreage_within_half_still_matches():
    """parcel_acres is a point query against this same layer and matches to
    four decimals. osm_acres is a digitised polygon and will not, so the
    tolerance has to admit a sizeable gap."""
    idx, _note = identify([('OWNER', 40.0)], 30.0)
    assert idx == 0


def test_an_acreage_off_by_more_than_half_does_not_match():
    idx, note = identify([('OWNER', 100.0)], 30.0)
    assert idx is None
    assert '100.0' in note


def test_the_floor_admits_a_small_site_whose_parcel_differs_by_a_few_acres():
    """Half of 5 acres is 2.5, which would reject a parcel 4 acres out on a
    site this size. The floor is what keeps small sites matchable."""
    idx, _note = identify([('OWNER', 12.0)], 5.0)
    assert idx == 0


def test_the_closest_parcel_wins_not_the_first_within_tolerance():
    idx, _note = identify([('FAR', 38.0), ('NEAR', 31.0)], 30.0)
    assert idx == 1


# --- edges ---------------------------------------------------------------

def test_no_parcels_means_no_match():
    idx, note = identify([], 30.0)
    assert idx is None
    assert note


def test_a_missing_candidate_acreage_means_no_match():
    """best_acres is parcel_acres combined with osm_acres, and a site can
    have neither. Matching against NaN would pick an arbitrary parcel."""
    idx, note = identify([('OWNER', 30.0)], float('nan'))
    assert idx is None, "a NaN base matched a parcel"
    assert 'acreage' in note


def test_a_none_candidate_acreage_means_no_match():
    idx, _note = identify([('OWNER', 30.0)], None)
    assert _note and _note is not None
    assert idx is None


def test_a_non_numeric_candidate_acreage_means_no_match():
    idx, _note = identify([('OWNER', 30.0)], 'not a number')
    assert idx is None


def test_a_zero_candidate_acreage_means_no_match():
    """Zero is not a measurement of a site, and matching against it would
    pick whichever parcel is smallest."""
    idx, _note = identify([('OWNER', 0.5), ('OWNER2', 30.0)], 0.0)
    assert idx is None


def test_a_negative_candidate_acreage_means_no_match():
    idx, _note = identify([('OWNER', 30.0)], -5.0)
    assert idx is None


# --- the vacancy basis ---------------------------------------------------

def test_the_vacancy_basis_is_derived_from_the_registry():
    """Not asserted as a constant. WV's layer has no land-use field, so the
    basis must read owner_name_only; a state whose layer has one must read
    the other, without this file being edited."""
    src = open(os.path.join(REPO, 'check_adjacent_land.py'), encoding='utf-8').read()
    assert 'HAS_LANDUSE_FIELD = bool(STATEWIDE_SVC.get("landuse"))' in src
    assert "'owner_name_only'" in src or '"owner_name_only"' in src
    from states import resolve
    svc = resolve.statewide_service(resolve.single_selected('test'))
    expected = ('owner_name_and_landuse' if svc.get('landuse')
                else 'owner_name_only')
    assert expected == 'owner_name_only', (
        "wv.json now declares a land-use field; this test's premise, that "
        "WV vacancy is an owner-name guess, no longer holds and the "
        "measurement behind it should be re-read")


def test_the_verdict_names_an_owner_name_only_vacancy():
    """A reader cannot tell a keyword match on an owner's name from a
    land-use classification by looking at the acreage."""
    fn = load_function('check_adjacent_land.py', 'expansion_verdict',
                       {'total_potential_acres': load_function(
                           'check_adjacent_land.py', 'total_potential_acres')})
    verdict = fn({'status': 'ok', 'base_acres': 28.0, 'vacant_acres': 137.8,
                  'same_owner_acres': 0.0, 'n_neighbors': 2,
                  'total_neighbor_acres': 165.2,
                  'vacancy_basis': 'owner_name_only',
                  'site_parcel_identified': True})
    assert 'STRONG' in verdict
    assert 'owner names only' in verdict, (
        f"the verdict does not disclose the vacancy basis: {verdict}")


def test_the_verdict_stays_quiet_when_a_landuse_field_exists():
    fn = load_function('check_adjacent_land.py', 'expansion_verdict',
                       {'total_potential_acres': load_function(
                           'check_adjacent_land.py', 'total_potential_acres')})
    verdict = fn({'status': 'ok', 'base_acres': 28.0, 'vacant_acres': 137.8,
                  'same_owner_acres': 0.0, 'n_neighbors': 2,
                  'total_neighbor_acres': 165.2,
                  'vacancy_basis': 'owner_name_and_landuse',
                  'site_parcel_identified': True})
    assert 'owner names only' not in verdict


def test_the_verdict_flags_an_unmatched_site_parcel():
    """The sum may double-count the site's own land, and the verdict is where
    a reader of the printed run would see it."""
    fn = load_function('check_adjacent_land.py', 'expansion_verdict',
                       {'total_potential_acres': load_function(
                           'check_adjacent_land.py', 'total_potential_acres')})
    verdict = fn({'status': 'ok', 'base_acres': 28.0, 'vacant_acres': 137.8,
                  'same_owner_acres': 0.0, 'n_neighbors': 3,
                  'total_neighbor_acres': 193.2,
                  'vacancy_basis': 'owner_name_only',
                  'site_parcel_identified': False,
                  'site_parcel_match_note': 'closest was 23475.98'})
    assert 'site parcel unmatched' in verdict
    assert '23475' in verdict


def test_a_failed_query_still_reads_as_unmeasured():
    fn = load_function('check_adjacent_land.py', 'expansion_verdict',
                       {'total_potential_acres': load_function(
                           'check_adjacent_land.py', 'total_potential_acres')})
    verdict = fn({'status': 'query_failed', 'query_status': 'http_error'})
    assert 'UNMEASURED' in verdict
    assert 'not a finding' in verdict


# --- the new CSV columns -------------------------------------------------

def test_the_csv_carries_the_provenance_columns():
    src = open(os.path.join(REPO, 'check_adjacent_land.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    cols = next((n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'cols'
                         for t in n.targets)), None)
    assert cols is not None, "the column list is gone"
    names = [e.value for e in cols.elts if isinstance(e, ast.Constant)]
    for required in ('vacancy_basis', 'site_parcel_identified',
                     'site_parcel_acres', 'total_potential_acres'):
        assert required in names, (
            f"{required} is not written to the CSV, so a reader of the file "
            f"cannot see how the acreage figures were arrived at. Columns: "
            f"{names}")


def test_the_status_alone_disqualifies_a_total(monkeypatch):
    """A mutation sweep found the status check redundant against the fixture
    it was tested with: the query_failed dict carried no base_acres, so
    deleting the status guard still returned None via the missing-base guard.
    Two guards both holding is fine; a test that cannot tell which one fired
    is not. This passes a failed result that DOES carry a base, so only the
    status check can refuse it."""
    fn = load_function('check_adjacent_land.py', 'total_potential_acres')
    assert fn({'status': 'query_failed', 'query_status': pp.HTTP_ERROR,
               'base_acres': 28.0, 'vacant_acres': 0.0,
               'same_owner_acres': 0.0}) is None, (
        "a query that never answered was given a total potential acreage. "
        "The number would read as a measurement of land nobody looked at")


def test_an_unrecognised_status_gets_no_total():
    """Fail closed. A status this function has not been taught about is not
    evidence that the sum is meaningful."""
    fn = load_function('check_adjacent_land.py', 'total_potential_acres')
    assert fn({'status': 'something_new', 'base_acres': 28.0,
               'vacant_acres': 1.0, 'same_owner_acres': 2.0}) is None
