"""
A score computed from a default must be distinguishable from a measured one.

TWO MEASURED THINGS
-------------------
1. UNKNOWN ACREAGE OUTSCORES A MEASURED SMALL ONE. score_parcel returns 2.5
   for a missing, non-numeric or non-finite acreage, and 0.0 for a parcel
   under 50 acres. A site whose parcel query FAILED therefore scores higher
   on this axis than a site measured at 30 acres. Six of the thirteen
   scorers impute that way, and nothing in the export said which rows rested
   on a default.

   The imputed VALUE is unchanged here. Scoring an unknown at zero buries a
   site that might be 600 acres, and nothing would prompt a recheck, whereas
   a slightly over-ranked site stays visible in a list a person reads. What
   is added is the provenance.

2. ONE PARCEL FIGURE IS NOT A SITE. In outputs/csv/top_candidates_wv.csv,
   DTE SMITH BRANCH carries parcel_acres of 23,475.98 with owner BLUESTONE
   RESOURCES INC, a coal landholding. enrich_columns.py's own comment records
   why: a point query "just returns whatever parcel polygon the query point
   happens to land in". The score is 5.0 either way, because the rubric tops
   out at 500 acres, so this does not move a ranking; it misrepresents the
   figure.

   No independent acreage exists to check it against: zero of the sixteen
   rows carry osm_acres. So the threshold derives from the rubric's own top
   band rather than from a comparison, and it is a flag and not a cap.

THE REFACTOR MUST BE OUTPUT-NEUTRAL
-----------------------------------
score_parcel was rewritten to read its bands from a named table so the
implausibility threshold could derive from the top one. A refactor that
changes a score is not a refactor, so the first test below pins the old
behaviour band by band, including the boundaries.
"""
import ast
import math
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def lift(*names):
    """Compile named top-level functions and constants out of
    score_and_export.py without importing it.

    The module cannot be imported: it reads a GeoPackage and runs the whole
    pipeline at import time.
    """
    import numpy as np
    import pandas as pd
    src = open(os.path.join(REPO, 'score_and_export.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    ns = {'np': np, 'pd': pd, '__name__': 'lifted'}
    wanted = set(names)
    body = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef,)) and node.name in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & wanted:
                body.append(node)
    exec(compile(ast.Module(body=body, type_ignores=[]),
                 'score_and_export.py', 'exec'), ns)
    missing = [n for n in names if n not in ns]
    assert not missing, f"{missing} not found at top level in score_and_export.py"
    return ns


@pytest.fixture(scope='module')
def ns2():
    return lift('PARCEL_BANDS', 'PARCEL_TOP_BAND_ACRES', 'PARCEL_UNKNOWN_SCORE',
                'PARCEL_IMPLAUSIBLE_MULTIPLE', 'PARCEL_IMPLAUSIBLE_ACRES',
                'parcel_acreage_is_measured', 'parcel_implausibility_flags')


@pytest.fixture(scope='module')
def ns():
    return lift('PARCEL_BANDS', 'PARCEL_TOP_BAND_ACRES', 'PARCEL_UNKNOWN_SCORE',
                'PARCEL_IMPLAUSIBLE_MULTIPLE', 'PARCEL_IMPLAUSIBLE_ACRES',
                'BROWNFIELD_SCORE', 'parcel_acreage_is_measured',
                'score_parcel', 'imputed_axes', '_is_present',
                'IMPUTED_SCORE_INPUTS')


# --- the refactor is output-neutral --------------------------------------

#: The pre-refactor function, transcribed from the commit that preceded it.
#: Every case below is checked against THIS rather than against a table
#: written from the new code, which would only prove the new code agrees
#: with itself.
def score_parcel_before(acres):
    try:
        a = float(acres)
    except (TypeError, ValueError):
        return 2.5
    if not math.isfinite(a) or a <= 0:
        return 2.5
    if a >= 500:  return 5.0
    if a >= 250:  return 4.0
    if a >= 100:  return 3.0
    if a >= 50:   return 2.0
    return 0.0


PARCEL_CASES = [
    0, 0.0, -1, -0.0001, 0.1, 1, 49, 49.999, 50, 50.001, 99, 100, 100.001,
    249, 250, 250.001, 499, 500, 500.001, 1000, 23475.98123558, 1e9,
    None, '', 'not a number', float('nan'), float('inf'), float('-inf'),
    '250', '500.0',
]


@pytest.mark.parametrize('value', PARCEL_CASES, ids=repr)
def test_score_parcel_is_unchanged_by_the_refactor(ns, value):
    assert ns['score_parcel'](value) == score_parcel_before(value), (
        f"score_parcel({value!r}) changed, so the refactor altered an output")


def test_the_boundary_cases_are_actually_in_the_case_list():
    """A control on the list above. If it held no band boundary, the
    parametrised test would pass for a function with the wrong comparison
    operators."""
    for floor in (50, 100, 250, 500):
        assert floor in PARCEL_CASES, f"{floor} is not exercised"
        assert floor - 1 in PARCEL_CASES or floor - 0.001 in PARCEL_CASES


def test_an_unknown_acreage_still_outscores_a_small_measured_one(ns):
    """Not a defect being left unfixed by oversight. Recorded so the choice
    is visible, and so a later change to it is deliberate: burying an
    unknown is the silent error, over-ranking one is the visible error."""
    assert ns['score_parcel'](None) > ns['score_parcel'](30.0)
    assert ns['score_parcel'](None) == ns['PARCEL_UNKNOWN_SCORE']


# --- the measured/imputed predicate --------------------------------------

@pytest.mark.parametrize('value', [50.0, 1, 0.5, '250', 23475.98])
def test_a_real_acreage_is_measured(ns, value):
    assert ns['parcel_acreage_is_measured'](value) is True


@pytest.mark.parametrize('value', [
    None, '', 'not a number', float('nan'), float('inf'), float('-inf'),
    0, 0.0, -1,
])
def test_everything_score_parcel_imputes_reads_as_unmeasured(ns, value):
    assert ns['parcel_acreage_is_measured'](value) is False
    assert ns['score_parcel'](value) == ns['PARCEL_UNKNOWN_SCORE']


def test_the_predicate_and_the_scorer_cannot_disagree(ns):
    """The predicate exists so the provenance column agrees with the score.
    If one says measured and the other imputes, the column lies."""
    for value in PARCEL_CASES:
        imputed_by_score = ns['score_parcel'](value) == ns['PARCEL_UNKNOWN_SCORE']
        measured = ns['parcel_acreage_is_measured'](value)
        # 2.5 is not a band value, so the only way to score it is to impute.
        assert imputed_by_score == (not measured), (
            f"{value!r}: score says imputed={imputed_by_score}, predicate "
            f"says measured={measured}")


# --- the implausibility threshold ----------------------------------------

def test_the_threshold_derives_from_the_rubrics_top_band(ns):
    """Not a second typed number. A literal here could drift from the band
    it is meant to be a multiple of."""
    assert ns['PARCEL_TOP_BAND_ACRES'] == ns['PARCEL_BANDS'][0][0]
    assert ns['PARCEL_IMPLAUSIBLE_ACRES'] == (
        ns['PARCEL_TOP_BAND_ACRES'] * ns['PARCEL_IMPLAUSIBLE_MULTIPLE'])


def test_the_dte_smith_branch_figure_is_above_the_threshold(ns):
    """The measured case, with its real number."""
    assert 23475.98123558 > ns['PARCEL_IMPLAUSIBLE_ACRES']


def test_every_other_acreage_in_the_shipped_output_is_below_it(ns):
    """A control. A threshold that flagged the whole portfolio would be
    useless, and one that flagged nothing would be untested."""
    import csv
    path = os.path.join(REPO, 'outputs', 'csv', 'top_candidates_wv.csv')
    if not os.path.exists(path):
        pytest.skip('no shipped output in this checkout')
    flagged = []
    for row in csv.DictReader(open(path)):
        raw = row.get('parcel_acres') or ''
        if not raw:
            continue
        if float(raw) > ns['PARCEL_IMPLAUSIBLE_ACRES']:
            flagged.append((row['Plant_Name'], float(raw)))
    assert len(flagged) == 1, (
        f"expected exactly the one coal landholding to flag, got {flagged}")
    assert 'DTE SMITH BRANCH' in flagged[0][0]


def test_the_flag_does_not_change_the_score(ns):
    """A cap would. The rubric already tops out at 500 acres, so the score is
    5.0 either side of the threshold, which is why a wrong threshold costs a
    label and not a ranking."""
    assert ns['score_parcel'](ns['PARCEL_IMPLAUSIBLE_ACRES'] - 1) == 5.0
    assert ns['score_parcel'](ns['PARCEL_IMPLAUSIBLE_ACRES'] + 1) == 5.0
    assert ns['score_parcel'](23475.98) == 5.0


# --- the imputed-axes column ---------------------------------------------

def frame(**cols):
    import pandas as pd
    return pd.DataFrame(cols)


def test_a_fully_measured_row_is_marked_on_nothing(ns):
    out = ns['imputed_axes'](frame(
        parcel_acres=[120.0], dist_to_pipeline_mi=[1.2],
        dist_to_water_mi=[0.4], flood_zone=['X'], seismic_ss=[0.18],
        brownfield_type=['Coal']))
    assert list(out) == ['']


def test_a_failed_parcel_query_is_named(ns):
    out = ns['imputed_axes'](frame(
        parcel_acres=[float('nan')], dist_to_pipeline_mi=[1.2],
        dist_to_water_mi=[0.4], flood_zone=['X'], seismic_ss=[0.18],
        brownfield_type=['Coal']))
    assert list(out) == ['score_parcel']


def test_several_imputed_axes_are_all_named(ns):
    out = ns['imputed_axes'](frame(
        parcel_acres=[None], dist_to_pipeline_mi=[None],
        dist_to_water_mi=[0.4], flood_zone=[''], seismic_ss=[float('nan')],
        brownfield_type=['Coal']))
    marks = list(out)[0].split(',')
    assert set(marks) == {'score_parcel', 'score_gas', 'score_flood_risk',
                          'score_seismic'}


def test_a_brownfield_type_outside_the_table_is_named(ns):
    """`.map(BROWNFIELD_SCORE).fillna(5)` gives an unrecognised type the same
    5 points as Biomass, which is a default dressed as a measurement."""
    out = ns['imputed_axes'](frame(
        parcel_acres=[120.0], dist_to_pipeline_mi=[1.2],
        dist_to_water_mi=[0.4], flood_zone=['X'], seismic_ss=[0.18],
        brownfield_type=['Something Nobody Scored']))
    assert list(out) == ['score_brownfield']


def test_a_known_brownfield_type_is_not_named(ns):
    for known in list(ns['BROWNFIELD_SCORE'])[:4]:
        out = ns['imputed_axes'](frame(
            parcel_acres=[120.0], dist_to_pipeline_mi=[1.2],
            dist_to_water_mi=[0.4], flood_zone=['X'], seismic_ss=[0.18],
            brownfield_type=[known]))
        assert list(out) == [''], f"{known!r} read as an unknown type"


def test_an_absent_input_column_marks_every_row(ns):
    """The silent case. If a whole column is missing, every row on that axis
    is a default, and saying nothing would be the error this file exists to
    stop."""
    out = ns['imputed_axes'](frame(parcel_acres=[120.0, 130.0]))
    for mark in out:
        assert 'no dist_to_pipeline_mi column' in mark
        assert 'no flood_zone column' in mark


def test_a_zero_distance_is_a_measurement_not_an_absence(ns):
    """0.0 miles to a pipeline is the best possible value. Treating it as
    missing would mark the strongest rows as imputed."""
    out = ns['imputed_axes'](frame(
        parcel_acres=[120.0], dist_to_pipeline_mi=[0.0],
        dist_to_water_mi=[0.0], flood_zone=['X'], seismic_ss=[0.0],
        brownfield_type=['Coal']))
    assert list(out) == ['']


def test_a_whitespace_only_value_is_an_absence(ns):
    out = ns['imputed_axes'](frame(
        parcel_acres=[120.0], dist_to_pipeline_mi=[1.2],
        dist_to_water_mi=[0.4], flood_zone=['   '], seismic_ss=[0.18],
        brownfield_type=['Coal']))
    assert list(out) == ['score_flood_risk']


def test_rows_are_marked_independently(ns):
    """One shared list object would mark every row with every other row's
    imputations."""
    out = ns['imputed_axes'](frame(
        parcel_acres=[120.0, None], dist_to_pipeline_mi=[None, 1.2],
        dist_to_water_mi=[0.4, 0.4], flood_zone=['X', 'X'],
        seismic_ss=[0.18, 0.18], brownfield_type=['Coal', 'Coal']))
    assert list(out) == ['score_gas', 'score_parcel']


def test_the_result_is_aligned_to_the_frames_index(ns):
    """The call site is `top["imputed_scores"] = imputed_axes(top)`, which
    aligns by LABEL. top's index is the rank, so a Series carrying a fresh
    RangeIndex writes NaN for every row whose rank is not 0..n-1, silently."""
    import pandas as pd
    f = frame(parcel_acres=[120.0, None], dist_to_pipeline_mi=[1.2, 1.2],
              dist_to_water_mi=[0.4, 0.4], flood_zone=['X', 'X'],
              seismic_ss=[0.18, 0.18], brownfield_type=['Coal', 'Coal'])
    f.index = [5, 16]
    out = ns['imputed_axes'](f)
    assert list(out.index) == [5, 16], (
        f"the result's index is {list(out.index)}, so assigning it back to "
        f"the frame would align wrongly")
    f['imputed_scores'] = out
    assert f.loc[16, 'imputed_scores'] == 'score_parcel'
    assert f.loc[5, 'imputed_scores'] == ''


def test_a_duplicate_index_does_not_lose_a_row(ns):
    """Indexing the per-row list by label returns a Series on a duplicate
    index, and the lookup-by-value variant marks only the first occurrence.
    `top`'s index is unique today; nothing in that file guarantees it, and
    the failure would land after every download had already run."""
    f = frame(parcel_acres=[120.0, None], dist_to_pipeline_mi=[1.2, 1.2],
              dist_to_water_mi=[0.4, 0.4], flood_zone=['X', 'X'],
              seismic_ss=[0.18, 0.18], brownfield_type=['Coal', 'Coal'])
    f.index = [7, 7]
    out = list(ns['imputed_axes'](f))
    assert out == ['', 'score_parcel'], (
        f"got {out}; the second row's imputed parcel score was lost or "
        f"attributed to the first")


# --- the parcel implausibility flag --------------------------------------

def test_the_flag_marks_only_the_oversized_parcel(ns2):
    out = list(ns2['parcel_implausibility_flags'](
        frame(parcel_acres=[23475.98, 504.71, None])))
    assert out == [True, False, False]


def test_the_flag_is_aligned_to_the_frames_index(ns2):
    f = frame(parcel_acres=[23475.98, 504.71])
    f.index = [3, 9]
    out = ns2['parcel_implausibility_flags'](f)
    assert list(out.index) == [3, 9]


def test_the_absent_column_fallback_is_also_aligned(ns2):
    """The .apply path carries the index for free; the fallback builds a
    Series by hand and has to be told. A mutation sweep found that only the
    first path was covered, because the alignment test used a frame that HAD
    the column. The call site assigns by label, so a fresh RangeIndex here
    writes NaN for every row whose rank is not 0..n-1."""
    f = frame(brownfield_type=['Coal', 'Coal'])
    f.index = [3, 9]
    out = ns2['parcel_implausibility_flags'](f)
    assert list(out.index) == [3, 9], (
        f"the fallback returned index {list(out.index)}, so assigning it "
        f"back to the frame would align wrongly")
    f['parcel_acres_implausible'] = out
    assert f['parcel_acres_implausible'].notna().all(), (
        "assigning the fallback produced NaN, so the column says nothing")


def test_an_absent_parcel_column_is_flagged_false_and_reported(ns2, capsys):
    """Unguarded this was a KeyError at the very end of a pipeline run, after
    every download and every service query had already succeeded."""
    out = ns2['parcel_implausibility_flags'](frame(brownfield_type=['Coal']))
    assert list(out) == [False]
    printed = capsys.readouterr().out
    assert 'no parcel_acres column' in printed
    assert 'not a finding' in printed, (
        "the message does not say that a missing column is not a finding "
        "that every acreage is plausible")


def test_the_flag_on_an_empty_frame_is_an_empty_bool_series(ns2):
    out = ns2['parcel_implausibility_flags'](frame(parcel_acres=[]))
    assert len(out) == 0


def test_an_empty_frame_does_not_raise(ns):
    out = ns['imputed_axes'](frame(
        parcel_acres=[], dist_to_pipeline_mi=[], dist_to_water_mi=[],
        flood_zone=[], seismic_ss=[], brownfield_type=[]))
    assert len(out) == 0


def test_every_imputing_scorer_is_covered(ns):
    """A control on the table. A scorer that imputes and is not listed is an
    axis whose defaults stay invisible, which is the whole defect."""
    covered = {score for score, _inp, _pred in ns['IMPUTED_SCORE_INPUTS']}
    assert covered == {'score_parcel', 'score_gas', 'score_water',
                       'score_flood_risk', 'score_seismic', 'score_brownfield'}


# --- the export carries both columns -------------------------------------

def test_the_export_carries_the_provenance_columns():
    src = open(os.path.join(REPO, 'score_and_export.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    cols = next(n.value for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'EXPORT_COLS'
                        for t in n.targets))
    names = [e.value for e in cols.elts if isinstance(e, ast.Constant)]
    for required in ('imputed_scores', 'parcel_acres_implausible',
                     'acres_source'):
        assert required in names, (
            f"{required} is not exported, so the provenance never reaches "
            f"the CSV a person reads")


def test_the_parcel_query_attributes_its_acreage():
    """acres_source was blank for all twelve WVGIS rows in the shipped CSV,
    so the one acreage source with a documented point-in-polygon limitation
    was the one source left unattributed."""
    src = open(os.path.join(REPO, 'enrich_columns.py'), encoding='utf-8').read()
    assert '"acres_source"] = source' in src, (
        "the parcel query does not record where its acreage came from")
