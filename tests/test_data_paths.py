"""
Every data file a pipeline script READS must be one some downloader WRITES.

WHY THIS FILE EXISTS
--------------------
On 2026-10-26 the Opportunity Zone block was found scoring every West
Virginia site 0 of 5, for as long as the fork had existed. Nothing was broken
at runtime. The downloader wrote a Census tract file under West Virginia's
FIPS; score_and_export.py looked for the same file under Tennessee's, the
state this repo was forked from. The consumer guards with
`if oz_csv.exists() and tract_zip.exists()` and falls through to
`opportunity_zone = False`, printing "Opportunity Zone data not found". So a
wrong PATH presented itself as a missing DOWNLOAD, and a 5-point axis quietly
went to zero for every candidate.

That shape is specific to a fork: the state token inside a Census filename is
the one part of a path a fork must change and that nothing checks. Every
consumer in this repo guards on exists(), which is correct for a missing
download and is exactly what makes a typo silent.

WHAT THIS DOES NOT DO
---------------------
It does not require every read to have a writer. Most reads are of files an
earlier pipeline stage produced, and demanding a downloader for those would
be wrong. The load-bearing check is the NEAR MISS: a name that differs from a
downloaded one only in its state token. That is a typo by construction,
because no downloader produces the file and a correctly-named sibling exists.
"""
import ast
import os
import re
from collections import defaultdict

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOWNLOADERS = ('download_data.py', 'download_westvirginia.py',
               'download_westvirginia_extras.py', 'download_gas_pipelines_wv.py')
CONSUMERS = ('build_candidates.py', 'filter_pipeline.py', 'enrich_candidates.py',
             'enrich_columns.py', 'enrich_retirement.py', 'score_and_export.py',
             'fetch_parcels.py', 'fetch_parcels_wv.py', 'fetch_epa_compliance.py',
             'check_adjacent_land.py')

DATA_FILE = re.compile(
    r'^[A-Za-z0-9_][A-Za-z0-9_.\-]*\.'
    r'(?:csv|zip|xlsx|geojson|gpkg|json|shp|parquet|CSV)$')

#: A state token inside a filename: a two-digit FIPS or a two-letter
#: abbreviation, delimited so it cannot match part of a longer word.
STATE_TOKEN = re.compile(r'(_\d{2}_|_[a-z]{2}_|_[a-z]{2}\.)')


def string_constants(filename, root=None):
    """Every string literal in a module, with its line number.

    Parsed rather than grepped, so a filename inside a COMMENT is not read as
    a path. That matters here: the comment recording why the Opportunity Zone
    path is now derived has to name the old filename, and a substring search
    over the source cannot tell that explanation from a regression.

    Bare string STATEMENTS (module, class and function docstrings) are
    excluded as well. On its own that exclusion is close to redundant --
    DATA_FILE must match the WHOLE literal, so a filename inside a sentence
    is already skipped -- and it is kept for the case the whole-match test
    misses: a docstring that is nothing but a filename.

    `root` defaults to the repository. It is an argument rather than a
    patched global because `tests/` has no __init__.py, so monkeypatching
    `tests.test_data_paths.REPO` can land on a second copy of this module and
    change nothing, which it did.
    """
    path = os.path.join(root or REPO, filename)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())

    prose = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            prose.add(id(node.value))

    return [(n.value, n.lineno) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in prose]


def data_filenames(files, root=None):
    found = defaultdict(list)
    for fn in files:
        for value, lineno in string_constants(fn, root=root):
            # A path literal here is one segment, e.g. "tl_2023_54_tract.zip".
            # Take the last segment so "a/b/c.csv" is handled either way.
            name = value.rsplit('/', 1)[-1]
            if DATA_FILE.match(name):
                found[name].append(f"{fn}:{lineno}")
    return found


def shape_of(name):
    """The filename with its state token blanked, so siblings collide."""
    return STATE_TOKEN.sub('_X_', name)


def near_misses(written, read):
    """Read names with no writer whose SHAPE a written name does have.

    A pure function over the two maps, so the detector can be tested against
    a fixture that contains a near miss. Testing it only against the live
    tree proves nothing while the tree is clean: a mutation sweep disabled
    this comparison entirely and the suite stayed green, because there was
    nothing for it to find.
    """
    by_shape = defaultdict(lambda: (set(), set()))
    for name in written:
        by_shape[shape_of(name)][0].add(name)
    for name in read:
        by_shape[shape_of(name)][1].add(name)

    out = []
    for _shape, (w, r) in sorted(by_shape.items()):
        orphans = r - w
        if w and orphans:
            for name in sorted(orphans):
                out.append((name, sorted(w)))
    return out


@pytest.fixture(scope='module')
def maps():
    return data_filenames(DOWNLOADERS), data_filenames(CONSUMERS)


# --- controls on the extraction ------------------------------------------

def test_the_extraction_finds_a_name_known_to_be_on_both_sides(maps):
    """Positive control. tl_2020_54_tabblock20.zip is written by
    download_westvirginia.py and read by score_and_export.py under exactly
    that name. If the extraction cannot see it, every absence this file
    reports is evidence of nothing."""
    written, read = maps
    control = 'tl_2020_54_tabblock20.zip'
    assert control in written, (
        f"{control} not found among downloader literals; the parse is broken")
    assert control in read, (
        f"{control} not found among consumer literals; the parse is broken")


def test_a_filename_inside_a_sentence_is_not_a_path(tmp_path):
    """The whole-literal match is what does most of the work. A filename
    mentioned in prose is part of a longer string and never matches."""
    (tmp_path / 'synthetic.py').write_text(
        '"""A docstring naming prose_only_54.csv, which is not a path."""\n'
        'import os\n'
        'PATH = os.path.join("dir", "real_literal_54.csv")\n',
        encoding='utf-8')
    names = data_filenames(('synthetic.py',), root=str(tmp_path))
    assert 'real_literal_54.csv' in names, (
        "the extraction misses a genuine path literal, so every absence it "
        "reports is evidence of nothing")
    assert 'prose_only_54.csv' not in names


def test_a_docstring_that_is_nothing_but_a_filename_is_not_a_path(tmp_path):
    """The case the whole-literal match cannot catch, and the only case the
    docstring exclusion is load-bearing for. Without the exclusion this
    docstring is byte-identical to a path literal."""
    (tmp_path / 'synthetic.py').write_text(
        '"""only_a_filename_54.csv"""\n'
        'VALUE = "a_real_path_54.csv"\n',
        encoding='utf-8')
    names = data_filenames(('synthetic.py',), root=str(tmp_path))
    assert 'a_real_path_54.csv' in names, "the extraction is broken"
    assert 'only_a_filename_54.csv' not in names, (
        "a docstring consisting of just a filename was read as a path")


def test_a_filename_in_a_comment_is_not_a_path(tmp_path):
    """Why this is parsed rather than grepped. score_and_export.py's comment
    about the fix names tl_2023_47_tract.zip, and a grep would report the
    explanation as the bug."""
    (tmp_path / 'synthetic.py').write_text(
        '# this used to say commented_out_47.csv\n'
        'VALUE = "live_54.csv"\n',
        encoding='utf-8')
    names = data_filenames(('synthetic.py',), root=str(tmp_path))
    assert 'live_54.csv' in names
    assert 'commented_out_47.csv' not in names


def test_the_state_token_pattern_actually_matches_one():
    """A control for shape_of. If the pattern matched nothing, every pair of
    names would have a distinct shape and the near-miss check could never
    fire, while reading as a clean result."""
    assert shape_of('tl_2023_54_tract.zip') == shape_of('tl_2023_47_tract.zip')
    assert shape_of('tl_2023_54_tract.zip') != shape_of('tl_2023_54_block.zip')
    assert shape_of('nri_counties.csv') == 'nri_counties.csv'


# --- controls on the detector itself --------------------------------------

def test_the_detector_finds_a_near_miss_in_a_fixture():
    """The detector has to be exercised against a tree that HAS one. The live
    tree is clean, so a mutation that disabled the comparison left the suite
    green: there was nothing for it to find."""
    found = near_misses({'tl_2023_54_tract.zip': ['dl.py:1']},
                        {'tl_2023_47_tract.zip': ['score.py:1']})
    assert found == [('tl_2023_47_tract.zip', ['tl_2023_54_tract.zip'])]


def test_the_detector_passes_a_matching_pair():
    found = near_misses({'tl_2023_54_tract.zip': ['dl.py:1']},
                        {'tl_2023_54_tract.zip': ['score.py:1']})
    assert found == []


def test_the_detector_ignores_a_read_with_no_same_shaped_writer():
    """An intermediate file an earlier stage produced. Flagging these would
    make the check useless noise."""
    found = near_misses({'tl_2023_54_tract.zip': ['dl.py:1']},
                        {'top_candidates_wv.csv': ['adjacent.py:1']})
    assert found == []


def test_the_detector_reports_every_orphan_of_one_shape():
    found = near_misses({'nri_54_counties.csv': ['dl.py:1']},
                        {'nri_47_counties.csv': ['a.py:1'],
                         'nri_01_counties.csv': ['b.py:1']})
    assert [n for n, _w in found] == ['nri_01_counties.csv', 'nri_47_counties.csv']


# --- the live tree --------------------------------------------------------

def test_no_consumer_reads_a_near_miss_of_a_downloaded_file(maps):
    """THE REGRESSION TEST."""
    written, read = maps
    found = near_misses(written, read)
    detail = [
        f"{name} (read at {', '.join(read[name])}) has no writer, but "
        f"{writers} is written at "
        f"{', '.join(sum((written[x] for x in writers), []))}"
        for name, writers in found]
    assert found == [], (
        "a consumer reads a filename no downloader produces, while a "
        "same-shaped file IS produced under a different state token. Every "
        "consumer guards on exists(), so this scores as missing data rather "
        "than failing:\n  " + "\n  ".join(detail))


# --- the specific fix -----------------------------------------------------

def test_the_opportunity_zone_tract_path_is_derived_from_the_registry():
    """A second literal is not a fix: two literals in two files cannot be
    kept in step by anyone noticing."""
    literals = {v for v, _ln in string_constants('score_and_export.py')}
    assert 'tl_2023_47_tract.zip' not in literals, "Tennessee's FIPS is back"
    with open(os.path.join(REPO, 'score_and_export.py'), encoding='utf-8') as f:
        src = f.read()
    assert 'f"tl_2023_{SCORED_STATE_FIPS}_tract.zip"' in src, (
        "the tract path is a literal again rather than derived from the "
        "registry's FIPS")
    assert 'resolve.require_fips' in src, (
        "require_fips raises for a state whose code is unfilled; plain fips() "
        "returns None and would build 'tl_2023_None_tract.zip'")


def test_the_scored_state_name_is_not_a_hardcoded_fork_label():
    """`print("RANKED CANDIDATES — TENNESSEE")` sat at the top of this repo's
    own results table. Cosmetic until someone pastes the table into a
    document, at which point it is a wrong-state label on real WV findings."""
    # SCREAMING CASE only, and deliberately so. 'Tennessee Gas Pipeline' is a
    # real interstate pipeline crossing West Virginia and a key in this file's
    # own diameter lookup table; it is a counterparty, not a fork label, and
    # an upper() comparison flags it. Do not widen this back.
    literals = {v for v, _ln in string_constants('score_and_export.py')}
    offenders = sorted(v for v in literals
                       if re.search(r'\bTENNESSEE\b', v))
    assert offenders == [], (
        f"a hardcoded state label is back in a live string: {offenders}. "
        f"The run's state comes from SCORED_STATE_NAME, which is resolved "
        f"from the registry")
    with open(os.path.join(REPO, 'score_and_export.py'), encoding='utf-8') as f:
        assert 'SCORED_STATE_NAME.upper()' in f.read()


def test_the_derived_values_resolve_for_the_default_selection():
    """The derivation must actually produce the WV values, not merely be
    syntactically derived. A wrong derivation is worse than the literal."""
    import sys
    sys.path.insert(0, REPO)
    from states import resolve
    state = resolve.single_selected('test_data_paths.py')
    assert state == 'WV'
    assert resolve.require_fips(state) == '54'
    assert f"tl_2023_{resolve.require_fips(state)}_tract.zip" == 'tl_2023_54_tract.zip'
    name = resolve.state_names([state])[state].replace('_', ' ')
    assert name.upper() == 'WEST VIRGINIA'
