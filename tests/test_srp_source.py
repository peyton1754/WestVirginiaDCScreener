"""
The EPA Redevelopment Mapper source must not cache its own failures.

WHAT WAS WRONG
--------------
Two defects in build_candidates.py's Source 5 block, the second worse than
the first.

1. `resp.json().get("features", [])` on an ArcGIS error body served with
   HTTP 200 returns an empty list. `resp.raise_for_status()` catches 4xx and
   5xx and cannot see that case at all. It is the same shape that hid the
   2026-10-26 parcel outage, in a file the fix had not reached.

2. `srp_raw.to_csv(SRP_CACHE, index=False)` ran unconditionally, so a failed
   fetch wrote a ZERO-ROW CACHE. Every later run took the cache branch,
   printed "Loaded 0 sites from cache" and skipped the source. One transient
   failure disabled an input permanently, and the only symptom was a line
   that reads like a successful cache hit.

The cost is not cosmetic: enrich_columns.py section 4b uses this source's
Property_Size to override parcel acreage, on the stated grounds that a
professionally assessed brownfield boundary beats a parcel point query. No
rows means no override, which understates acreage into score_parcel.

WHY THESE TESTS ARE STRUCTURAL
------------------------------
build_candidates.py cannot be imported: it pulls in geopandas and does its
work at module level, so the Source 5 block is not a function to call. The
checks below parse it instead. A structural check is weaker than a
behavioural one and is said so plainly here; what it can do is prove the
guard exists and sits BEFORE the write, which is the ordering the defect was.
"""
import ast
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = 'build_candidates.py'


@pytest.fixture(scope='module')
def src():
    with open(os.path.join(REPO, TARGET), encoding='utf-8') as f:
        return f.read()


@pytest.fixture(scope='module')
def tree(src):
    return ast.parse(src)


def test_the_parse_control(tree):
    """If the module stops parsing, every absence below is vacuous."""
    assert any(isinstance(n, ast.Assign) for n in ast.walk(tree))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert 'SRP_CACHE' in names, "the Source 5 block is not in this file"


def test_the_swallowing_idiom_is_gone(src):
    """Both halves of it. raise_for_status cannot see an HTTP-200 error body,
    and .get("features", []) turns that body into an empty result."""
    assert 'resp.raise_for_status()' not in src
    assert 'resp.json().get("features"' not in src
    assert '.json().get("features", [])' not in src, (
        "a response body is being read without being classified, which is "
        "how an ArcGIS error becomes an empty result set")


def test_the_srp_fetch_goes_through_source_probe(src, tree):
    """Available is not the same as wired in. source_probe's own tests prove
    the classifier works; this proves this block uses it."""
    assert 'import source_probe' in src
    fetches = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == 'fetch'
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == 'source_probe']
    assert len(fetches) == 1, (
        f"expected exactly one source_probe.fetch in {TARGET} (the SRP "
        f"download), found {len(fetches)}")
    node = fetches[0]
    attempts = next((kw.value for kw in node.keywords if kw.arg == 'attempts'), None)
    assert isinstance(attempts, ast.Constant) and attempts.value >= 2, (
        "the SRP download does not retry, so a single 503 loses the source "
        "for the run")


def test_the_srp_outcomes_are_recorded_and_reported(tree):
    attrs = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
             and n.value.id == 'srp_outcomes'}
    assert 'record' in attrs, "nothing records the SRP query outcomes"
    assert attrs & {'report', 'lines'}, (
        "srp_outcomes is write-only, so its tallies reach nobody")
    assert 'failed' in attrs, (
        "nothing reads srp_outcomes.failed, so a partial result cannot be "
        "told from a complete one and would be cached as an answer")


def _linenos(tree, predicate):
    return sorted(n.lineno for n in ast.walk(tree) if predicate(n))


def test_the_cache_write_is_preceded_by_the_failure_guard(tree):
    """THE ORDERING TEST. The defect was a write that ran whatever happened.
    A guard that sits after the write is no guard."""
    writes = _linenos(tree, lambda n: (
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == 'to_csv'
        and any(isinstance(a, ast.Name) and a.id == 'SRP_CACHE' for a in n.args)))
    assert len(writes) == 1, f"expected one SRP cache write, found {len(writes)}"

    guards = _linenos(tree, lambda n: (
        isinstance(n, ast.If)
        and any(isinstance(x, ast.Attribute) and x.attr == 'failed'
                and isinstance(x.value, ast.Name) and x.value.id == 'srp_outcomes'
                for x in ast.walk(n.test))))
    assert guards, "no `if srp_outcomes.failed` guard exists at all"
    assert min(guards) < writes[0], (
        f"the failure guard is at line {min(guards)} and the cache write at "
        f"{writes[0]}: the write is not protected by it")


def test_the_failure_guard_raises_rather_than_warning(tree):
    """Continuing past a partial result would reach the write below it."""
    guard = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.If)
                 and any(isinstance(x, ast.Attribute) and x.attr == 'failed'
                         and isinstance(x.value, ast.Name)
                         and x.value.id == 'srp_outcomes'
                         for x in ast.walk(n.test)))
    assert any(isinstance(n, ast.Raise) for n in ast.walk(guard)), (
        "the guard detects a partial result and does not stop, so the cache "
        "write below it still runs")


def test_a_zero_row_cache_is_not_trusted(src, tree):
    """The repair half. A poisoned cache already on disk must be re-fetched,
    not read as 'this state has no large brownfields'."""
    assert '_srp_cache_usable' in src, (
        "nothing distinguishes a usable cache from a zero-row one")
    # The usable flag must be set only under a length test, not by existence.
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == '_srp_cache_usable'
                       for t in n.targets)
               and isinstance(n.value, ast.Constant) and n.value.value is True]
    assert len(assigns) == 1, (
        f"expected exactly one place that marks the cache usable, found "
        f"{len(assigns)}")
    enclosing = [n for n in ast.walk(tree)
                 if isinstance(n, ast.If)
                 and any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                         and x.func.id == 'len' for x in ast.walk(n.test))
                 and any(a.lineno == assigns[0].lineno
                         for a in ast.walk(n) if isinstance(a, ast.Assign))]
    assert enclosing, (
        "the cache is marked usable without testing how many rows it holds, "
        "so a zero-row file from a failed download is believed again")


def test_losing_the_source_is_reported_as_a_failure(src):
    """It is one of six candidate sources and the only EPA acreage override.
    `[warn]` put it at the same volume as a cosmetic notice."""
    assert '[warn] Redevelopment Mapper source failed' not in src
    assert '[FAIL] Redevelopment Mapper source unavailable' in src
    assert 'not a finding' in src, (
        "the message does not say that a missing source is not a finding "
        "about the sites, which is the distinction this whole change is for")
