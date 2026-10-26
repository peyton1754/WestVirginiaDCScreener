"""
The GHG parent-company stopword list was Tennessee's, and a missing stopword
drops West Virginia sites without saying so.

HOW THE LIST IS USED
--------------------
COMMON_WORDS strips non-distinguishing tokens from each active GHG reporter's
name to build a "company root". Any candidate whose name CONTAINS a root is
then excluded as an active GHG reporter. The asymmetry is the whole point:

    a token IN the list      cannot become part of a root  -> site kept
    a token MISSING from it  can become a root on its own   -> site dropped

The list carried TENNESSEE, NASHVILLE, MEMPHIS, KNOXVILLE, CHATTANOOGA and
CLARKSVILLE, which is the fork's lineage, and no West Virginia equivalents.
On a WV run a reporter named "CHARLESTON <something generic>" leaves
CHARLESTON as the only surviving token, long enough to become a root by
itself, and every candidate with CHARLESTON in its name is excluded. The
exclusion printed as a count and never named a site.

WHICH DIRECTION TO DEFAULT
--------------------------
Over-exclusion drops a real candidate and nothing reports it. Under-exclusion
keeps an active reporter in the pool, where the retirement-confidence
multiplier grades it ACTIVE_WARNING and a person reading the top candidates
sees the name. The visible error is the cheaper one, so geographic tokens are
stopworded, and both directions are now reported.
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def lift(*names):
    """Compile named top-level definitions out of build_candidates.py.

    The module cannot be imported: it reads shapefiles and runs the whole
    candidate build at import time.
    """
    import pandas as pd
    from states import resolve
    src = open(os.path.join(REPO, 'build_candidates.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    ns = {'pd': pd, 'resolve': resolve, '__name__': 'lifted',
          'TARGET_STATES': resolve.selected()}
    wanted = set(names)
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign):
            if {t.id for t in node.targets if isinstance(t, ast.Name)} & wanted:
                body.append(node)
    exec(compile(ast.Module(body=body, type_ignores=[]),
                 'build_candidates.py', 'exec'), ns)
    missing = [n for n in names if n not in ns]
    assert not missing, f"{missing} not found at top level"
    return ns


@pytest.fixture(scope='module')
def ns():
    return lift('GENERIC_NAME_WORDS', 'CITY_COLUMNS', 'state_name_tokens',
                'city_name_tokens', 'generic_name_tokens')


def frame(**cols):
    import pandas as pd
    return pd.DataFrame(cols)


# --- the fork's tokens are gone, and ours are derived --------------------

def test_the_tennessee_city_tokens_are_gone(ns):
    for token in ('TENNESSEE', 'NASHVILLE', 'MEMPHIS', 'KNOXVILLE',
                  'CHATTANOOGA', 'CLARKSVILLE'):
        assert token not in ns['GENERIC_NAME_WORDS'], (
            f"{token} is still a hardcoded generic token, which is the "
            f"fork's lineage rather than this repo's subject")


def test_the_state_name_tokens_come_from_the_registry(ns):
    """WEST and VIRGINIA must arrive without this file naming either."""
    tokens = ns['state_name_tokens'](['WV'])
    assert tokens == {'WEST', 'VIRGINIA'}


def test_the_state_tokens_follow_whichever_state_is_selected(ns):
    """The point of deriving them. A different state's run must not get
    West Virginia's tokens."""
    tokens = ns['state_name_tokens'](['TN'])
    assert 'TENNESSEE' in tokens
    assert 'VIRGINIA' not in tokens


def test_a_single_word_state_name_yields_one_token(ns):
    """TX is in the registry; OH is not, which is why this uses a declared
    state rather than any plausible-looking code."""
    assert ns['state_name_tokens'](['TX']) == {'TEXAS'}


def test_a_two_word_state_name_yields_both_tokens(ns):
    """ND's nhd_name is North_Dakota. Splitting on the underscore is what
    makes both halves non-identifying, and NORTH is already in the industry
    list while DAKOTA is not."""
    assert ns['state_name_tokens'](['ND']) == {'NORTH', 'DAKOTA'}


def test_a_multi_state_run_gets_every_state_name(ns):
    assert ns['state_name_tokens'](['WV', 'VA']) == {'WEST', 'VIRGINIA'}
    assert ns['state_name_tokens'](['WV', 'TX']) == {'WEST', 'VIRGINIA', 'TEXAS'}


# --- city tokens come from the data -------------------------------------

def test_city_tokens_are_read_from_the_facility_files_own_column(ns):
    tokens, note = ns['city_name_tokens'](
        frame(CITY_NAME=['CHARLESTON', 'HUNTINGTON', 'SAINT ALBANS']))
    assert 'CHARLESTON' in tokens
    assert 'HUNTINGTON' in tokens
    assert 'ALBANS' in tokens
    assert 'CITY_NAME' in note


def test_short_city_tokens_are_skipped(ns):
    """A three-letter token is too short to be a root anyway (the root
    builder requires four), so including it only adds noise."""
    tokens, _note = ns['city_name_tokens'](frame(CITY_NAME=['ELK', 'CHARLESTON']))
    assert 'ELK' not in tokens
    assert 'CHARLESTON' in tokens


def test_a_comma_in_a_city_value_does_not_produce_a_glued_token(ns):
    tokens, _note = ns['city_name_tokens'](frame(CITY_NAME=['SAINT ALBANS, WV']))
    assert 'ALBANS' in tokens
    assert not any(',' in t for t in tokens)


def test_a_missing_city_column_is_reported_rather_than_assumed(ns):
    """An empty city set means geographic tokens CAN become roots. That is a
    real change in behaviour and must not be silent."""
    tokens, note = ns['city_name_tokens'](frame(PRIMARY_NAME=['A PLANT']))
    assert tokens == set()
    assert 'no city column' in note
    assert 'can therefore become company roots' in note


#: Written out here rather than read from the module. A loop over
#: ns['CITY_COLUMNS'] asserts that whatever the tuple happens to contain is
#: recognised, so shrinking the tuple to one entry left that test green.
EXPECTED_CITY_COLUMNS = ('CITY_NAME', 'CITY', 'FAC_CITY', 'LOCALITY_NAME')


@pytest.mark.parametrize('col', EXPECTED_CITY_COLUMNS)
def test_every_candidate_city_column_is_tried(ns, col):
    """FRS extracts differ between vintages and between the facility and
    the alternate-name files, so more than one spelling has to be tried. A
    column that is not recognised means no city tokens at all, which means
    city names can become company roots."""
    tokens, note = ns['city_name_tokens'](frame(**{col: ['CHARLESTON']}))
    assert 'CHARLESTON' in tokens, f"{col} was not recognised"
    assert col in note


def test_the_recognised_columns_are_the_expected_ones(ns):
    assert tuple(ns['CITY_COLUMNS']) == EXPECTED_CITY_COLUMNS, (
        "the city-column list changed. If that is deliberate, update "
        "EXPECTED_CITY_COLUMNS too; if a column was dropped, city tokens "
        "silently stop being collected for files that use it")


def test_the_most_specific_city_column_wins(ns):
    """FRS files can carry more than one. CITY_NAME is the facility's own."""
    tokens, note = ns['city_name_tokens'](
        frame(CITY_NAME=['CHARLESTON'], CITY=['WRONGPLACE']))
    assert 'CHARLESTON' in tokens
    assert 'WRONGPLACE' not in tokens
    assert 'CITY_NAME' in note


def test_null_cities_do_not_raise(ns):
    tokens, _note = ns['city_name_tokens'](frame(CITY_NAME=[None, 'CHARLESTON']))
    assert tokens == {'CHARLESTON'}


# --- the combined set ----------------------------------------------------

def test_the_combined_set_carries_all_three_sources(ns, capsys):
    tokens = ns['generic_name_tokens'](frame(CITY_NAME=['CHARLESTON']))
    assert 'COMPANY' in tokens, "the industry words are missing"
    assert 'VIRGINIA' in tokens, "the state name tokens are missing"
    assert 'CHARLESTON' in tokens, "the city tokens are missing"
    printed = capsys.readouterr().out
    assert 'name tokens treated as generic' in printed, (
        "the token count is not reported, so a run cannot show whether the "
        "city column was found")


def test_the_charleston_case_no_longer_produces_a_root(ns):
    """THE REGRESSION TEST, run through the real root-building rule.

    A reporter named "CHARLESTON TERMINAL COMPANY" used to leave CHARLESTON
    as the only surviving token, which is six characters and so became a root
    on its own, excluding every candidate with CHARLESTON in its name."""
    tokens = ns['generic_name_tokens'](frame(CITY_NAME=['CHARLESTON']))
    words = [w for w in 'CHARLESTON TERMINAL COMPANY'.split()
             if w not in tokens and len(w) >= 4]
    assert words == [], (
        f"{words} would become a company root, and any candidate whose name "
        f"contains it would be dropped as an active GHG reporter")


def test_a_real_company_name_still_produces_a_root(ns):
    """The control. If everything were stopworded, no reporter would exclude
    anything and the filter would be dead while reading as clean."""
    tokens = ns['generic_name_tokens'](frame(CITY_NAME=['CHARLESTON']))
    words = [w for w in 'PECHINEY ROLLED PRODUCTS LLC'.split()
             if w not in tokens and len(w) >= 4]
    assert len(words) >= 2, (
        f"a distinctive company name produced {words}, too few to build a "
        f"two-token root, so the GHG filter would exclude nothing")
    assert words[0] == 'PECHINEY'


def test_a_wv_city_that_is_also_a_company_name_is_stopworded(ns):
    """Weirton Steel is in Weirton. Stopwording the city means that reporter
    yields no root, so it is not excluded BY NAME: the under-exclusion
    direction, which is the visible one. The run reports it."""
    tokens = ns['generic_name_tokens'](frame(CITY_NAME=['WEIRTON']))
    words = [w for w in 'WEIRTON STEEL'.split()
             if w not in tokens and len(w) >= 4]
    assert words == [], "the premise of this test no longer holds"


# --- both directions are reported ---------------------------------------

def test_the_run_reports_single_token_roots_and_rootless_reporters():
    """Over-exclusion was invisible: the roots printed only as a count. A
    single-token root is the dangerous shape, because it matches a substring
    of any candidate name."""
    src = open(os.path.join(REPO, 'build_candidates.py'), encoding='utf-8').read()
    assert 'GHG company root(s) built from' in src
    assert 'yielded no root' in src, (
        "the under-exclusion direction is not reported")
    assert 'will exclude any candidate whose name contains them' in src, (
        "a single-token root is not called out, so the over-exclusion "
        "direction stays invisible")

    # REACHABILITY, not presence. Changing `if _single:` to `if False:` left
    # the string in the file and the warning unreachable, and a text
    # assertion could not tell the difference.
    tree = ast.parse(src)
    guarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body_src = ' '.join(
            c.value for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str))
        if 'will exclude any candidate whose name contains them' in body_src:
            guarded.append(node)
    assert guarded, "the single-token-root warning is not inside any branch"
    tests = {ast.dump(n.test) for n in guarded}
    assert any(isinstance(n.test, ast.Name) and n.test.id == '_single'
               for n in guarded), (
        f"the warning's branch is not guarded by the single-token root set, "
        f"so it is either unreachable or always fires. Guard(s): {tests}")


def test_the_stopword_list_is_no_longer_a_literal_at_the_call_site():
    src = open(os.path.join(REPO, 'build_candidates.py'), encoding='utf-8').read()
    assert 'COMMON_WORDS = generic_name_tokens(fac)' in src, (
        "the call site still builds its own set, so the derivation is not "
        "what the filter uses")
