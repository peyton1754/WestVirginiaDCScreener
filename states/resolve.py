"""
Resolve the pipeline's state-specific constants from the registry.

WHY THIS LAYER EXISTS
---------------------
`schema.py` reads and validates a state config. This module answers the
question the pipeline scripts actually ask: "what value do I use for THIS
run?" Keeping the two apart matters because the scripts cannot be imported in
a test environment: `download_data.py` starts downloading at import time and
`build_candidates.py` pulls in the whole geopandas stack. So the resolution
logic lives here, where it is pure stdlib and directly testable, and each
script's module-level constant becomes a one-line call into it.

THE DEFAULT MUST NOT CHANGE ANY OUTPUT
--------------------------------------
Every function here returns, for West Virginia, exactly the literal it
replaced. `selected()` defaults to `('WV',)` with no environment set, so a run
that does not opt in is byte-identical to the pre-registry pipeline. That is
the property that makes this migration safe to land before anything has been
run end to end, and `tests/test_resolve.py` asserts it per constant.

WHAT IS DELIBERATELY NOT MIGRATED
---------------------------------
Two constants stay as literals in their scripts, each for a stated reason, and
each gains a test that fails if it drifts from the registry.

1. `score_and_export.STATE_SCORE` carries GA, NC and OH, which are not on the
   combined map and have no registry entry. Migrating it would either drop
   those three scores (every GA site would silently score 0 on the state axis,
   because the consumer is `.map(STATE_SCORE).fillna(0)`) or require declaring
   three states nobody intends to screen. It is already pinned by
   `test_every_sourced_state_score_reaches_the_registry`.

2. `download_gas_pipelines_wv.BBOX` is the registry bbox padded by roughly 40
   miles, but the pad was applied by hand to a ROUNDED bbox and is not uniform
   on any edge: the four offsets are 0.7555, 0.7015, 0.8190 and 0.6612 degrees.
   No single formula reproduces the literal, so computing it here would change
   the envelope the HIFLD query uses and therefore change which pipelines are
   downloaded. A refactor that alters downloaded data is not a refactor.
   `bbox_query_string()` exists for a state that needs one built, and
   `test_gas_bbox_still_contains_the_registry_bbox` guards the WV literal by
   containment instead.
"""
from __future__ import annotations

import os

from . import schema

#: Environment variable naming the states to screen, comma or space separated.
SELECT_ENV = 'SCREENER_STATES'

#: What a run screens when nothing opts in. Keeping this at WV is what makes
#: the migration output-neutral; see the module docstring.
DEFAULT_SELECTION = ('WV',)

#: Readiness levels a run may proceed with. `declared` is refused because its
#: required fields are unfilled and the failure would otherwise surface deep
#: in the run as a wrong-state filter rather than at startup.
RUNNABLE = (schema.READY_FULL, schema.READY_PARTIAL)


def _parse_selection(raw):
    """Split a selection string into upper-case codes, order preserved."""
    if raw is None:
        return list(DEFAULT_SELECTION)
    codes = [c.strip().upper() for c in raw.replace(',', ' ').split()]
    return [c for c in codes if c]


def selected(env=None):
    """Return the state codes this run screens, refusing any that cannot run.

    Reads `SCREENER_STATES` (comma or space separated) and falls back to
    `DEFAULT_SELECTION`. Raises `StateConfigError` naming every unfilled field
    if a selected state is only `declared`, so an incomplete config fails at
    startup with the remaining work listed rather than part way through a
    multi-gigabyte download.
    """
    env = os.environ if env is None else env
    raw = env.get(SELECT_ENV)
    codes = _parse_selection(raw)
    if not codes:
        raise schema.StateConfigError(
            f"{SELECT_ENV} is set to {raw!r}, which names no states. "
            f"Unset it to screen {', '.join(DEFAULT_SELECTION)}.")

    seen, ordered = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            ordered.append(c)

    problems = []
    for code in ordered:
        cfg = schema.load(code)  # raises for an unknown or malformed state
        level, reasons = schema.readiness(cfg)
        if level not in RUNNABLE:
            problems.append(f"{code} is {level} and cannot be screened yet:\n    "
                            + "\n    ".join(reasons))
    if problems:
        raise schema.StateConfigError(
            "selected states are not runnable:\n  " + "\n  ".join(problems))
    return ordered


def single_selected(context, env=None):
    """Return the one selected state, refusing a multi-state run.

    `check_adjacent_land.py` and `enrich_columns.py` hold their parcel service
    in a module-level constant, so they can carry exactly one state's service
    however many states are selected. In a multi-state run they would quietly
    query the first state's parcel layer for every site, and the wrong owner
    and acreage would land in the output looking like real data.

    That limitation predates the registry: the literal those scripts used had
    the same shape. What changes here is that it fails at startup and says
    which script needs rewriting, instead of producing plausible wrong rows.
    """
    codes = selected(env=env)
    if len(codes) > 1:
        # The explanation stays GENERIC. An earlier version listed all
        # three consumers and what each holds, which read well and made the
        # "names the script" test unfalsifiable: a mutation that dropped
        # {context} entirely left every consumer's filename in the string.
        raise schema.StateConfigError(
            f"{context} resolves one state's constants at module level (a "
            f"parcel service, a Census FIPS, a state name) and cannot screen "
            f"{len(codes)} states at once ({', '.join(codes)}). Run it once "
            f"per state, or rewire it to resolve them per site before "
            f"selecting more than one.")
    return codes[0]


def state_names(codes=None):
    """Map each state code to its NHD filename form.

    Replaces `download_data.STATE_NAMES`. The value feeds
    `NHD_H_{name}_State_Shape.zip`, and for a state whose `nhd_name` was
    derived rather than looked up the URL either resolves on the first download
    or 404s immediately, which is a loud first-step failure.
    """
    codes = selected() if codes is None else codes
    return {c: schema.load(c)['nhd_name'] for c in codes}


def statewide_service(abbr):
    """Return the statewide parcel service config, or None if the state has none.

    Replaces the `STATEWIDE_SVC` literal in BOTH `check_adjacent_land.py` and
    `enrich_columns.py`. Those two copies had already diverged: the
    `enrich_columns` copy was missing the `landuse` key. That divergence was
    latent rather than active, because `enrich_columns` never reads `landuse`,
    but it is exactly the drift a second copy exists to produce and no test
    covered it. One source ends it.
    """
    cfg = schema.load(abbr)
    if not schema.has_parcel_service(cfg):
        return None
    return dict(cfg['parcels']['statewide_service'])


def county_overrides(abbr):
    """Return per-county parcel endpoints for states whose statewide layer is
    incomplete. Empty for WV, whose WVGIS composite covers all 55 counties.

    Replaces `check_adjacent_land.COUNTY_OVERRIDES`. Tennessee, which that
    script was originally written for, needed six.
    """
    cfg = schema.load(abbr)
    return dict((cfg.get('parcels') or {}).get('county_overrides') or {})


def bbox(abbr):
    """Return [west, south, east, north] for a state, or None if unfilled."""
    v = schema.load(abbr).get('bbox')
    return list(v) if v else None


def bbox_query_string(abbr, pad_deg=0.0, ndigits=None):
    """Return a `west,south,east,north` string, padded outward by `pad_deg`.

    Padding is applied symmetrically and rounded OUTWARD when `ndigits` is
    given, so the result always contains the unpadded bbox. It never shrinks
    the envelope, because a query box smaller than the state silently drops
    features near the border and the loss looks like absence of data.

    This does NOT reproduce `download_gas_pipelines_wv.BBOX`; see the module
    docstring for why that literal is pinned rather than migrated.
    """
    v = bbox(abbr)
    if v is None:
        raise schema.StateConfigError(
            f"{abbr.upper()} has no bbox filled in, so no query envelope can be built")
    if pad_deg < 0:
        raise ValueError(f"pad_deg must not be negative, got {pad_deg}")
    w, s, e, n = v[0] - pad_deg, v[1] - pad_deg, v[2] + pad_deg, v[3] + pad_deg
    if ndigits is not None:
        f = 10 ** ndigits
        import math
        w, s = math.floor(w * f) / f, math.floor(s * f) / f
        e, n = math.ceil(e * f) / f, math.ceil(n * f) / f
    return f"{w},{s},{e},{n}"


def fips(abbr):
    """Return the two-digit state FIPS code as a string, or None if unfilled.

    Null is a real and expected answer for nine of the ten declared states. A
    caller that needs it must handle None rather than treat the absence as a
    default, because a wrong FIPS screens the wrong state's facilities and
    every downstream number inherits it silently.
    """
    return schema.load(abbr).get('fips')


def require_fips(abbr):
    """Return the FIPS code, raising with the remaining work if it is unfilled."""
    v = fips(abbr)
    if v is None:
        raise schema.StateConfigError(
            f"{abbr.upper()} has no FIPS code in the registry. Look it up from the "
            f"Census ANSI state table and record the source in the config; do not "
            f"fill it from memory.")
    return v
