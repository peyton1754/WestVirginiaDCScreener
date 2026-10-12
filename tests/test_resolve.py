"""
Tests for the resolve layer, and for the migration being output-neutral.

THE LOAD-BEARING IDEA
---------------------
`tests/test_states.py` pins the registry against literals that still live in
the pipeline scripts. This file pins the OTHER direction: once a script's
literal is replaced by a call into `resolve`, the literal is gone and that
pin-down assertion can no longer read it. So the pre-migration value is
recorded here, once, as `PRE_MIGRATION`, and every resolver is asserted to
return it for West Virginia.

Those values were MEASURED from the scripts before the rewiring, by parsing
each module with `ast` at commit d02d98f, not recalled. Each entry below names
the file and the constant it came from. If a resolver ever stops reproducing
one of them, this migration changed the pipeline's behavior, which it is not
allowed to do.

`test_rewired_constants_are_no_longer_literals` is the other half: it asserts
by source inspection that each migrated constant is now a Call rather than a
literal, so a resolver could not pass these tests while the script quietly
kept using its own copy.
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from states import resolve, schema  # noqa: E402

PARCEL_URL = ('https://services.wvgis.wvu.edu/arcgis/rest/services/'
              'Planning_Cadastre/WV_Parcels/MapServer/0/query')

# Measured at d02d98f, before any consumer was rewired. See the module docstring.
PRE_MIGRATION = {
    # download_data.py:40 and :42
    'STATES': ['WV'],
    'STATE_NAMES': {'WV': 'West_Virginia'},
    # build_candidates.py:41
    'TARGET_STATES': ['WV'],
    # check_adjacent_land.py:22 -- the copy that carried every key
    'STATEWIDE_SVC': {
        'url': PARCEL_URL, 'owner': 'FullOwnerName',
        'acres': 'Acres_C', 'landuse': None,
    },
    # check_adjacent_land.py:27
    'COUNTY_OVERRIDES': {},
    # enrich_columns.py:155 -- the DIVERGED copy, missing 'landuse'
    'STATEWIDE_SVC_enrich_columns': {
        'url': PARCEL_URL, 'owner': 'FullOwnerName', 'acres': 'Acres_C',
    },
    # download_gas_pipelines_wv.py:24 -- pinned by containment, not migrated
    'BBOX': '-83.4,36.5,-76.9,41.3',
}

# Constants migrated to the registry, as (filename, constant name).
REWIRED = [
    ('download_data.py', 'STATES'),
    ('download_data.py', 'STATE_NAMES'),
    ('build_candidates.py', 'TARGET_STATES'),
    ('check_adjacent_land.py', 'STATEWIDE_SVC'),
    ('check_adjacent_land.py', 'COUNTY_OVERRIDES'),
    ('enrich_columns.py', 'STATEWIDE_SVC'),
]


def assignment(filename, name):
    """Return the AST value node of a module-level assignment, without running it."""
    tree = ast.parse(open(os.path.join(REPO, filename), encoding='utf-8').read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node.value
    raise KeyError(f"{name} not found at module level in {filename}")


# --- the default selection must change nothing ----------------------------

def test_default_selection_is_exactly_wv():
    """The whole safety argument for landing this before any end-to-end run."""
    assert resolve.selected(env={}) == PRE_MIGRATION['STATES']


def test_default_selection_ignores_an_empty_environment_variable():
    with pytest.raises(schema.StateConfigError):
        resolve.selected(env={resolve.SELECT_ENV: '   '})


@pytest.mark.parametrize('raw,expected', [
    ('TN', ['TN']),
    ('wv', ['WV']),
    ('WV,TN', ['WV', 'TN']),
    ('WV TN', ['WV', 'TN']),
    ('WV, TN , WV', ['WV', 'TN', 'WV']),  # parsing keeps duplicates
])
def test_selection_parsing(raw, expected):
    assert resolve._parse_selection(raw) == expected


def test_selection_collapses_duplicates_and_preserves_order():
    """Deduplication belongs in selected(), not the parser: the parser's job is
    to report what was asked for."""
    assert resolve.selected(env={resolve.SELECT_ENV: 'WV, WV'}) == ['WV']


def test_selection_refuses_a_declared_state_and_names_its_gaps():
    """TN is declared: it has no FIPS. The refusal must say so at startup
    rather than let a 1 to 2 GB download run and filter the wrong state."""
    with pytest.raises(schema.StateConfigError) as e:
        resolve.selected(env={resolve.SELECT_ENV: 'TN'})
    msg = str(e.value)
    assert 'TN' in msg and 'fips' in msg, msg


def test_selection_refuses_an_unknown_state():
    with pytest.raises(schema.StateConfigError):
        resolve.selected(env={resolve.SELECT_ENV: 'QQ'})


def test_selection_accepts_wv_because_it_is_full():
    assert resolve.selected(env={resolve.SELECT_ENV: 'WV'}) == ['WV']


def test_selection_reads_the_real_environment_when_none_is_passed(monkeypatch):
    monkeypatch.delenv(resolve.SELECT_ENV, raising=False)
    assert resolve.selected() == ['WV']


# --- the single-state guard -----------------------------------------------

def test_single_selected_returns_the_one_state():
    assert resolve.single_selected('x', env={resolve.SELECT_ENV: 'WV'}) == 'WV'


def test_single_selected_refuses_two_states_and_names_the_script(monkeypatch):
    """A multi-state run would query the first state's parcel layer for every
    site, and the wrong owner and acreage would land in the output looking
    like real data. This must fail at startup instead.

    The count branch is tested with `selected` stubbed, because WV is the only
    runnable state in the registry today: a real two-state selection is
    refused by the readiness check first, so it exercises the wrong branch.
    `test_single_selected_still_validates_readiness_first` covers that
    ordering, and the two together reach both branches.
    """
    monkeypatch.setattr(resolve, 'selected', lambda env=None: ['WV', 'TN'])
    with pytest.raises(schema.StateConfigError) as e:
        resolve.single_selected('enrich_columns.py')
    msg = str(e.value)
    assert 'enrich_columns.py' in msg, "the refusal must name the script to rewire"
    assert 'WV, TN' in msg, "the refusal must list which states were selected"


def test_single_selected_still_validates_readiness_first():
    """TN alone is declared, so the readiness refusal must win over the count
    check rather than the state slipping through as 'only one state'."""
    with pytest.raises(schema.StateConfigError) as e:
        resolve.single_selected('x', env={resolve.SELECT_ENV: 'TN'})
    assert 'fips' in str(e.value)


@pytest.mark.parametrize('filename', ['check_adjacent_land.py', 'enrich_columns.py'])
def test_the_single_state_consumers_use_the_guard(filename):
    """Both scripts hold their parcel service in a module-level constant, so
    both must be guarded. Without this, one could be rewired to plain
    selected()[0] and silently regain the mis-attribution bug."""
    src = open(os.path.join(REPO, filename), encoding='utf-8').read()
    assert 'resolve.single_selected(' in src, (
        f"{filename} resolves a parcel service without the multi-state guard")
    assert 'resolve.selected()[0]' not in src, (
        f"{filename} takes the first selected state without guarding the rest")


# --- every resolver reproduces the literal it replaced --------------------

def test_state_names_reproduces_the_literal():
    assert resolve.state_names(['WV']) == PRE_MIGRATION['STATE_NAMES']


def test_statewide_service_reproduces_the_complete_literal():
    assert resolve.statewide_service('WV') == PRE_MIGRATION['STATEWIDE_SVC']


def test_statewide_service_is_a_superset_of_the_diverged_copy():
    """The enrich_columns copy was missing 'landuse'. One source now serves
    both, and this asserts the diverged copy's keys all survive with their
    values intact, so rewiring it changes nothing it actually read."""
    one = resolve.statewide_service('WV')
    diverged = PRE_MIGRATION['STATEWIDE_SVC_enrich_columns']
    assert set(diverged) < set(one), "the diverged copy must be a strict subset"
    for k, v in diverged.items():
        assert one[k] == v, k


def test_county_overrides_reproduces_the_literal():
    assert resolve.county_overrides('WV') == PRE_MIGRATION['COUNTY_OVERRIDES']


def test_statewide_service_is_none_for_a_state_without_one():
    assert resolve.statewide_service('TN') is None


def test_county_overrides_is_empty_rather_than_raising_for_a_parcelless_state():
    assert resolve.county_overrides('TN') == {}


def test_a_mutated_service_dict_does_not_leak_back_into_the_registry(monkeypatch):
    """Resolvers must hand back a copy, so a consumer that mutates its config
    cannot corrupt a later read in the same process.

    `schema.load` is stubbed to return ONE shared object, which is the only way
    this test can fail. Written against the real loader it passed whether or
    not the resolvers copied anything, because `load` re-reads the JSON from
    disk on every call and there was no shared object to corrupt. A mutation
    sweep caught that: deleting both `dict(...)` calls left the suite green.

    So the copies are insurance against `load` ever gaining a cache, and this
    is the shape that actually tests them.
    """
    shared = {
        'abbr': 'WV', 'name': 'West Virginia', 'nhd_name': 'West_Virginia',
        'fips': '54', 'bbox': [-82.6447, 37.2015, -77.719, 40.6388],
        'state_score': 11, 'derived': [],
        'parcels': {
            'statewide_service': {'url': PARCEL_URL, 'owner': 'FullOwnerName',
                                  'acres': 'Acres_C', 'landuse': None},
            'county_overrides': {},
        },
    }
    monkeypatch.setattr(resolve.schema, 'load', lambda abbr: shared)

    svc = resolve.statewide_service('WV')
    svc['owner'] = 'MUTATED'
    assert resolve.statewide_service('WV')['owner'] == 'FullOwnerName'

    ovr = resolve.county_overrides('WV')
    ovr['Kanawha'] = 'MUTATED'
    assert resolve.county_overrides('WV') == {}


# --- the rewiring actually happened ---------------------------------------

@pytest.mark.parametrize('filename,name', REWIRED)
def test_rewired_constants_are_no_longer_literals(filename, name):
    """Without this, a resolver could return the right value while the script
    kept using its own copy, and every test above would still pass."""
    node = assignment(filename, name)
    assert isinstance(node, ast.Call), (
        f"{filename}:{name} is still a literal; the resolver is not wired in")


@pytest.mark.parametrize('filename,name', REWIRED)
def test_rewired_constants_call_into_the_resolve_layer(filename, name):
    node = assignment(filename, name)
    src = ast.unparse(node)
    assert 'resolve.' in src, f"{filename}:{name} is a call but not into resolve: {src}"


# The expression each rewired constant is now assigned, lifted from the live
# source, paired with the pre-migration value it must still produce.
WIRED_EXPECTATIONS = [
    ('download_data.py', 'STATES', 'STATES'),
    ('download_data.py', 'STATE_NAMES', 'STATE_NAMES'),
    ('build_candidates.py', 'TARGET_STATES', 'TARGET_STATES'),
    ('check_adjacent_land.py', 'STATEWIDE_SVC', 'STATEWIDE_SVC'),
    ('check_adjacent_land.py', 'COUNTY_OVERRIDES', 'COUNTY_OVERRIDES'),
    ('enrich_columns.py', 'STATEWIDE_SVC', 'STATEWIDE_SVC'),
]


@pytest.mark.parametrize('filename,name,expected_key', WIRED_EXPECTATIONS)
def test_the_wired_expression_evaluates_to_the_pre_migration_value(
        filename, name, expected_key, monkeypatch):
    """Evaluate the actual expression now in each script and check its result.

    None of these scripts can be imported in a test environment:
    `download_data.py` starts downloading at import time and the others pull in
    geopandas. So the expression is lifted out of the source with `ast` and
    evaluated on its own, with the same names in scope that the script has at
    that point. That is the closest thing available here to running them, and
    it is what proves the wiring is not merely syntactically a call into
    `resolve` but actually produces the value it replaced.

    What this does NOT cover, stated rather than implied: the rest of each
    script. These four files have never been run end to end in this container,
    because the pipeline's sources are blocked by its egress proxy. The first
    real run is still owed.
    """
    monkeypatch.delenv(resolve.SELECT_ENV, raising=False)
    node = assignment(filename, name)
    scope = {'resolve': resolve}
    # check_adjacent_land.py assigns _STATE before the constants that use it.
    if filename == 'check_adjacent_land.py':
        scope['_STATE'] = eval(ast.unparse(assignment(filename, '_STATE')), scope)
    # download_data.py's STATE_NAMES is resolved from its own STATES.
    if name == 'STATE_NAMES':
        scope['STATES'] = eval(ast.unparse(assignment(filename, 'STATES')), scope)
    value = eval(ast.unparse(node), scope)
    assert value == PRE_MIGRATION[expected_key], (
        f"{filename}:{name} now evaluates to {value!r}, "
        f"not the {PRE_MIGRATION[expected_key]!r} it replaced")


def test_the_diverged_parcel_literal_is_gone_from_the_tree():
    """The parcel URL may appear only in the registry, this file, and the
    scripts that fetch it directly. A second dict literal carrying it is the
    drift this migration exists to end."""
    hits = []
    for fn in os.listdir(REPO):
        if not fn.endswith('.py'):
            continue
        tree = ast.parse(open(os.path.join(REPO, fn), encoding='utf-8').read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                if 'owner' in keys and 'acres' in keys and 'url' in keys:
                    hits.append(f"{fn}:{node.lineno}")
    assert hits == [], f"parcel-service dict literals still in the tree: {hits}"


# --- the constants deliberately NOT migrated, pinned instead --------------

def test_gas_bbox_still_contains_the_registry_bbox():
    """`download_gas_pipelines_wv.BBOX` is hand-padded and is not migrated,
    because no formula reproduces it and recomputing it would change which
    pipelines get downloaded. Pin it by containment: the query envelope must
    still cover the whole state, with margin on every edge."""
    literal = ast.literal_eval(assignment('download_gas_pipelines_wv.py', 'BBOX'))
    assert literal == PRE_MIGRATION['BBOX'], "the literal moved; re-derive the margin"
    qw, qs, qe, qn = (float(x) for x in literal.split(','))
    w, s, e, n = resolve.bbox('WV')
    assert qw < w and qs < s and qe > e and qn > n, (
        f"query envelope {literal} does not contain the registry bbox "
        f"{resolve.bbox('WV')}")
    # The comment claims roughly 40 miles, which is about 0.58 degrees of
    # latitude. Assert a floor well below that rather than the exact figure,
    # since the four edges were padded unevenly by hand.
    assert min(w - qw, s - qs, qe - e, qn - n) > 0.5


def test_state_score_is_not_migrated_and_keeps_its_unmapped_states():
    """GA, NC and OH are scored upstream and are not on the combined map, so
    STATE_SCORE stays a literal. The consumer is `.map(...).fillna(0)`, so
    dropping a score would silently zero that state's axis rather than fail."""
    node = assignment('score_and_export.py', 'STATE_SCORE')
    scores = ast.literal_eval(node)
    assert {'GA', 'NC', 'OH'} <= set(scores)
    assert set(scores) - set(schema.all_codes()) == {'GA', 'NC', 'OH'}


# --- bbox_query_string ----------------------------------------------------

def test_bbox_query_string_is_exact_without_padding():
    assert resolve.bbox_query_string('WV') == '-82.6447,37.2015,-77.719,40.6388'


def test_bbox_query_string_rounds_outward_so_it_never_shrinks():
    """Rounding inward would drop features near the border, and the loss would
    look like absence of data rather than a clipped query."""
    out = resolve.bbox_query_string('WV', pad_deg=0.7, ndigits=1)
    qw, qs, qe, qn = (float(x) for x in out.split(','))
    w, s, e, n = resolve.bbox('WV')
    assert qw <= w - 0.7 and qs <= s - 0.7
    assert qe >= e + 0.7 and qn >= n + 0.7


def test_bbox_query_string_refuses_a_state_with_no_bbox(monkeypatch):
    monkeypatch.setattr(resolve, 'bbox', lambda abbr: None)
    with pytest.raises(schema.StateConfigError):
        resolve.bbox_query_string('TN', pad_deg=0.5)


def test_bbox_query_string_refuses_negative_padding():
    with pytest.raises(ValueError):
        resolve.bbox_query_string('WV', pad_deg=-0.1)


# --- fips -----------------------------------------------------------------

def test_fips_returns_wv_and_none_elsewhere():
    assert resolve.fips('WV') == '54'
    assert resolve.fips('TN') is None


def test_require_fips_raises_with_the_remaining_work_named():
    assert resolve.require_fips('WV') == '54'
    with pytest.raises(schema.StateConfigError) as e:
        resolve.require_fips('TN')
    assert 'Census' in str(e.value), "the refusal must say where to look it up"
