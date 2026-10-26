"""
The state tier is a reference value, not a scored axis.

WHY IT WAS REMOVED, measured rather than argued (Peyton, 2026-10-26):

1. It was 20 of 120 points, tied for the largest axis, above substation
   voltage at 15 and double water, metro and utility rate.
2. Within a single-state run it is a CONSTANT. All 16 rows of the shipped
   top_candidates_wv.csv carried 11.
3. It still moved ranks, because total_score = infra_score x
   retirement_multiplier, so the constant was scaled per site: 11.0 at a
   1.00 multiplier, 7.7 at 0.70. Dropping it moved 5 of 16 rank positions.
   An invented number was being scaled by a measured one.
4. It partly double-counted. Its own entries cited power cost in cents per
   kWh, which score_utility_rate derives per county from EIA 861, and fiber,
   which score_fiber derives from FCC Form 477.
5. No value in it is sourced. Every registry state records notes.sources
   for `fips`; not one records one for `state_score`.
6. It bought no cross-state comparability, which was the only argument for
   keeping it. The ten-state map reads total_score per state, but nine of
   ten are frozen extracts from separate forks, and three Louisiana sites
   score above this rubric's own 120-point ceiling.

AND IT WAS GATING FIVE STATES. `state_score` was in schema.REQUIRED, so
AL, KY, LA, MS and ND read as `declared`, which resolve.selected() refuses.
An unsourced hand-typed opinion was the single thing keeping five states out
of the pipeline.
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from states import resolve, schema  # noqa: E402


def source(filename='score_and_export.py'):
    with open(os.path.join(REPO, filename), encoding='utf-8') as f:
        return f.read()


def literals(filename='score_and_export.py'):
    """Every string literal, excluding docstrings and bare string statements.

    Parsed, so the comment block recording WHY the tier was removed, which
    necessarily names the old column, is not read as a live reference.
    """
    tree = ast.parse(source(filename))
    prose = {id(n.value) for n in ast.walk(tree)
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in prose}


def assignment(name, filename='score_and_export.py'):
    tree = ast.parse(source(filename))
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets):
            return n
    raise AssertionError(f"{name} not assigned in {filename}")


# --- the axis is out of the sum ------------------------------------------

def infra_score_terms():
    """Every column name summed into infra_score, read from the AST."""
    node = assignment('infra_score')
    return {s.slice.value for s in ast.walk(node)
            if isinstance(s, ast.Subscript)
            and isinstance(s.slice, ast.Constant)
            and isinstance(s.slice.value, str)}


def test_the_state_tier_is_not_summed_into_the_score():
    """THE CHANGE. A grep for the column name would also match the comment
    recording why it was removed, so this reads the sum itself."""
    terms = infra_score_terms()
    assert 'score_state' not in terms, (
        "the state tier is back in infra_score, so an invented constant is "
        "again being scaled by each site's retirement multiplier")
    assert 'state_tier_reference' not in terms, (
        "the renamed column is being summed, which is the same defect under "
        "a new name")


def test_the_control_that_the_sum_is_actually_being_read():
    """If infra_score_terms() returned nothing, the assertion above would
    pass for a file that still sums the tier."""
    terms = infra_score_terms()
    assert len(terms) >= 10, f"only found {len(terms)} terms: {sorted(terms)}"
    for expected in ('score_gas', 'score_substation', 'score_parcel',
                     'score_water', 'score_metro', 'score_utility_rate'):
        assert expected in terms, f"{expected} is missing from the sum"


def test_the_axis_count_matches_the_hundred_point_rubric():
    """Twelve axes, not thirteen."""
    assert len(infra_score_terms()) == 12, (
        f"the sum has {len(infra_score_terms())} terms; the 100-point rubric "
        f"has 12 axes")


# --- the rubric and the threshold ----------------------------------------

def test_the_docstring_says_one_hundred_points():
    doc = ast.get_docstring(ast.parse(source()))
    assert '100 pts total' in doc
    assert '120 pts total' not in doc
    assert 'State tier' not in doc.split('NOT an axis')[0], (
        "the state tier is still listed among the scored axes")
    assert 'NOT an axis' in doc, (
        "the docstring does not record that the tier was removed, so the "
        "next reader sees a 100-point rubric with no explanation")


def test_the_threshold_was_rescaled_not_left_alone():
    """28 of 120 is the same fraction as 23.33 of 100. Leaving 28 on a
    100-point scale would silently tighten the filter by a fifth."""
    node = assignment('MIN_SCORE')
    assert isinstance(node.value, ast.Constant)
    assert node.value.value == 23, (
        f"MIN_SCORE is {node.value.value}; the rescaled equivalent of 28 of "
        f"120 on a 100-point scale is 23")
    assert 'Rescaled from 28' in source(), (
        "the rescale is not explained, so a later reader cannot tell 23 from "
        "an arbitrary pick")


# --- the column is emitted, under a name that cannot read as a score -----

def test_the_tier_is_still_emitted():
    """Removed from the score, not deleted. A later cross-state comparison
    should not have to re-derive the intent."""
    assert 'state_tier_reference' in literals()
    cols = assignment('EXPORT_COLS')
    names = [e.value for e in cols.value.elts if isinstance(e, ast.Constant)]
    assert 'state_tier_reference' in names, (
        "the tier is computed and never exported, which loses it entirely")
    assert 'score_state' not in names


def test_no_live_reference_to_the_old_column_name_survives():
    """The old name in a live string would have a downstream extractor or a
    spreadsheet reading it as one of the scored axes."""
    assert 'score_state' not in literals()


def test_the_reference_table_is_still_there_with_its_reasons():
    """Keeping the numbers without keeping why they are not scored is how a
    later session puts them back."""
    node = assignment('STATE_SCORE')
    keys = [k.value for k in node.value.keys]
    assert 'WV' in keys and len(keys) >= 8
    src = source()
    assert 'REFERENCE table, not a scored axis' in src
    assert 'double-counted' in src
    # The CLAIM, not a token near it. A mutation that reversed
    # "No value here is sourced" to "Values are fine" left the words
    # 'notes.sources' in place one line below, and this test stayed green.
    assert 'No value here is sourced' in src, (
        "the comment no longer records that these values are unsourced, "
        "which is the reason a reader should not trust them")


# --- the registry gate ---------------------------------------------------

def test_state_score_no_longer_gates_readiness():
    assert 'state_score' not in schema.REQUIRED, (
        "state_score is required again, so five states go back to `declared` "
        "and an unsourced opinion gates them out of the pipeline")


def test_it_is_still_validated_where_present():
    """Not required is not unvalidated. A string there would reach
    `.map(STATE_SCORE)` and compare against ints."""
    problems = schema.validate({
        'abbr': 'ZZ', 'name': 'Nowhere', 'nhd_name': 'Nowhere',
        'fips': '99', 'bbox': [-90.0, 34.0, -81.0, 37.0],
        'state_score': 'high',
    })
    assert any('state_score' in p for p in problems), (
        f"a non-numeric state_score passed validation: {problems}")


@pytest.mark.parametrize('code', ['AL', 'KY', 'LA', 'MS', 'ND'])
def test_the_five_gated_states_are_now_runnable(code):
    """The measured consequence. Each of these read `declared` purely because
    state_score was unfilled, and `declared` is refused by selected()."""
    level, _reasons = schema.readiness(schema.load(code))
    assert level in resolve.RUNNABLE, (
        f"{code} is {level!r}, so resolve.selected() still refuses it")


def test_no_state_is_declared_any_more():
    declared = [c for c, cfg in sorted(schema.load_all().items())
                if schema.readiness(cfg)[0] == schema.READY_DECLARED]
    assert declared == [], f"still declared: {declared}"


def test_the_registry_and_the_reference_table_still_agree():
    """Where both carry a value they must match, so the reference table does
    not drift from the registry now that nothing scores against it."""
    node = assignment('STATE_SCORE')
    table = {k.value: v.value for k, v in zip(node.value.keys, node.value.values)}
    for code, cfg in sorted(schema.load_all().items()):
        if cfg.get('state_score') is not None and code in table:
            assert cfg['state_score'] == table[code], (
                f"{code}: registry {cfg['state_score']} vs table {table[code]}")
