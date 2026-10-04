"""
Tests for the state registry.

The load-bearing one is test_wv_registry_pins_the_live_literals. The registry was
EXTRACTED from constants that still live in the pipeline scripts, so until every
consumer is rewired there are two copies of the same values. That test reads both
sides and fails if they drift, which is the whole safety net for the migration.
It parses the scripts with ast rather than importing them, because importing
download_data.py starts downloading.

Run: python3 -m pytest tests/ -q
"""
import ast
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from states import schema  # noqa: E402


def module_literal(filename, name):
    """Read a module-level literal assignment without executing the module."""
    tree = ast.parse(open(os.path.join(REPO, filename), encoding='utf-8').read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise KeyError(f"{name} not found at module level in {filename}")


def good_config():
    return {
        'abbr': 'ZZ', 'name': 'Example', 'nhd_name': 'Example',
        'fips': '99', 'bbox': [-90.0, 34.0, -81.0, 37.0], 'state_score': 10,
        'parcels': {'statewide_service': {
            'url': 'https://example.gov/arcgis/rest/services/Parcels/MapServer/0/query',
            'owner': 'Owner', 'acres': 'Acres', 'landuse': None},
            'county_overrides': {}},
    }


# --- positive controls ----------------------------------------------------
# If these fail the validator has become too strict and every "problems is
# non-empty" assertion below means nothing.

def test_a_well_formed_config_passes():
    assert schema.validate(good_config()) == []


def test_a_well_formed_config_is_full():
    level, reasons = schema.readiness(good_config())
    assert level == schema.READY_FULL
    assert reasons == []


# --- the validator must reject what it exists to reject --------------------

@pytest.mark.parametrize('fips', [54, '5', '544', 'WV', 5.4])
def test_non_two_digit_string_fips_is_rejected(fips):
    """FIPS is a two-character STRING. Leading zeros are significant: Alabama is
    '01' and an integer 1 silently becomes the wrong lookup key."""
    cfg = good_config(); cfg['fips'] = fips
    assert any('fips' in p for p in schema.validate(cfg)), f"fips={fips!r} passed"


@pytest.mark.parametrize('bbox', [
    [-81.0, 34.0, -90.0, 37.0],   # east/west swapped
    [-90.0, 37.0, -81.0, 34.0],   # north/south swapped
    [-90.0, 34.0, -81.0],         # too short
    [-200.0, 34.0, -81.0, 37.0],  # off the planet
    [-90.0, 5.0, -81.0, 37.0],    # south of the US
])
def test_malformed_bbox_is_rejected(bbox):
    cfg = good_config(); cfg['bbox'] = bbox
    assert any('bbox' in p for p in schema.validate(cfg)), f"bbox={bbox!r} passed"


def test_lowercase_or_long_abbr_is_rejected():
    for bad in ('wv', 'W', 'WVA', '', None):
        cfg = good_config(); cfg['abbr'] = bad
        assert any('abbr' in p for p in schema.validate(cfg)), f"abbr={bad!r} passed"


def test_http_parcel_url_is_rejected():
    cfg = good_config()
    cfg['parcels']['statewide_service']['url'] = 'http://example.gov/x'
    assert any('https' in p for p in schema.validate(cfg))


def test_validator_can_fail():
    """Mutation control: a validator that always returns [] must not pass here.

    Without this, every assertion above could be satisfied by a validator broken
    in the other direction, and every '== []' by one that always returns [].
    """
    assert schema.validate(good_config()) == []
    assert schema.validate({'abbr': 'zz', 'fips': 54}) != []


# --- readiness ------------------------------------------------------------

def test_missing_required_field_makes_a_state_declared_and_names_it():
    cfg = good_config(); cfg['fips'] = None
    level, reasons = schema.readiness(cfg)
    assert level == schema.READY_DECLARED
    assert any('fips' in r for r in reasons), "the unfilled field must be named"


def test_no_parcel_service_is_partial_not_broken():
    """A state without a parcel service is still worth screening. Only the
    acreage enrichment and the adjacent-land step lose their source."""
    cfg = good_config(); cfg['parcels'] = None
    level, reasons = schema.readiness(cfg)
    assert level == schema.READY_PARTIAL
    assert any('adjacent-land' in r for r in reasons)


# --- the shipped registry -------------------------------------------------

def test_every_shipped_config_is_well_formed():
    for code in schema.all_codes():
        assert schema.validate(schema.load(code)) == [], code


def test_wv_is_fully_ready():
    level, reasons = schema.readiness(schema.load('WV'))
    assert level == schema.READY_FULL, reasons


def test_no_state_carries_a_fips_that_was_not_sourced():
    """WV's FIPS is the only one in this repo's source. Any other state showing
    a FIPS means someone filled it from memory, which this registry forbids.
    When a FIPS is genuinely looked up, add it here with its source."""
    sourced = {'WV': '54'}
    for code, cfg in schema.load_all().items():
        if cfg['fips'] is not None:
            assert code in sourced and cfg['fips'] == sourced[code], (
                f"{code} carries fips {cfg['fips']!r} with no recorded source")


def test_declared_states_name_their_gaps():
    for code, cfg in schema.load_all().items():
        level, reasons = schema.readiness(cfg)
        if level == schema.READY_DECLARED:
            assert reasons, f"{code} is not runnable but names no gap"


def test_load_rejects_an_unknown_state():
    with pytest.raises(schema.StateConfigError):
        schema.load('QQ')


# --- the pin-down: registry must equal the literals still in the scripts ----

def test_wv_registry_pins_the_live_literals():
    """Until every consumer reads the registry, these values exist twice. This
    fails the moment either copy changes, which is what makes the migration
    safe to do one script at a time."""
    wv = schema.load('WV')

    assert wv['nhd_name'] == module_literal('download_data.py', 'STATE_NAMES')['WV']
    assert wv['state_score'] == module_literal('score_and_export.py', 'STATE_SCORE')['WV']

    svc = module_literal('check_adjacent_land.py', 'STATEWIDE_SVC')
    assert wv['parcels']['statewide_service'] == svc
    assert wv['parcels']['county_overrides'] == module_literal(
        'check_adjacent_land.py', 'COUNTY_OVERRIDES')


def test_every_sourced_state_score_reaches_the_registry():
    """STATE_SCORE already carries eight states. Any of them that is also a
    declared state must carry the same score here, so the registry cannot
    quietly disagree with the scorer."""
    scores = module_literal('score_and_export.py', 'STATE_SCORE')
    for code, cfg in schema.load_all().items():
        if code in scores:
            assert cfg['state_score'] == scores[code], (
                f"{code}: registry {cfg['state_score']} vs STATE_SCORE {scores[code]}")


def test_registry_covers_every_state_on_the_combined_map():
    """The map renders ten states. All ten must be declared here, or a state can
    appear on the map with no config behind it."""
    expected = {'WV', 'TN', 'MS', 'ND', 'KY', 'AL', 'TX', 'LA', 'VA', 'AR'}
    assert set(schema.all_codes()) >= expected, (
        f"missing configs for {sorted(expected - set(schema.all_codes()))}")


# --- the nhd_name derivation ----------------------------------------------

def test_nhd_derivation_reproduces_wv():
    """The positive control for the rule. WV is the only state whose NHD
    filename form is known from this repo's own source, so if the rule stops
    reproducing it the rule is wrong for everyone."""
    known = module_literal('download_data.py', 'STATE_NAMES')['WV']
    assert schema.derive_nhd_name('West Virginia') == known == 'West_Virginia'


def test_derivation_handles_single_and_multi_word_names():
    assert schema.derive_nhd_name('Texas') == 'Texas'
    assert schema.derive_nhd_name('North Dakota') == 'North_Dakota'
    assert schema.derive_nhd_name('  Kentucky  ') == 'Kentucky'
    assert schema.derive_nhd_name(None) is None
    assert schema.derive_nhd_name('') is None


def test_derived_values_are_labelled_as_derived():
    """A derived value must never read as looked up. The registry file keeps a
    null and load() marks what it filled."""
    raw = json.load(open(os.path.join(schema.REGISTRY_DIR, 'tn.json'), encoding='utf-8'))
    assert raw['nhd_name'] is None, "registry file must not carry a derived value"

    tn = schema.load('TN')
    assert tn['nhd_name'] == 'Tennessee'
    assert 'nhd_name' in tn['derived']

    _level, reasons = schema.readiness(tn)
    assert any('derived by rule' in r for r in reasons), (
        "readiness must disclose that nhd_name was derived, not verified")


def test_wv_nhd_name_is_sourced_not_derived():
    wv = schema.load('WV')
    assert wv['nhd_name'] == 'West_Virginia'
    assert 'nhd_name' not in wv.get('derived', []), (
        "WV's nhd_name is in the source and must not be marked derived")
