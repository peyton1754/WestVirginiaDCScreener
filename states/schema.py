"""
State configuration schema for the multi-state screening pipeline.

WHY THIS EXISTS
---------------
This pipeline was already written to handle many states and then configured for
exactly one. The evidence is in the code it inherited: `download_data.py` holds
`STATES = ["WV"]` as a list beside a `STATE_NAMES` map, `build_candidates.py`
derives its state slug from a `TARGET_STATES` set, `score_and_export.py` scores
eight states, and `check_adjacent_land.py` carries an empty `COUNTY_OVERRIDES`
and a comment saying it was originally written for Tennessee. The machinery is
multi-state; only the configuration is single-state, and it is scattered across
eight module-level constants in seven files.

The DCScreener family's answer to a new state was to fork the whole repo. Nine
of those forks now exist, all unreachable, each with its own copy of the same
~6,400 lines, and each extractor pointing at a path on one developer's machine.
Forking is what let them rot independently. This registry is the alternative:
one pipeline, states as data.

WHAT A STATE NEEDS, AND WHAT IT CAN DO WITHOUT
----------------------------------------------
Most of this pipeline reads NATIONAL datasets and filters them by state: EIA-860
retired generators, EPA FRS and TRI, HIFLD substations and transmission, Census
TIGER, FEMA NRI. Those need nothing per state but a FIPS code, a name and a
bounding box.

A few sources are genuinely per state. Only one of them is actually fetched: the
statewide parcel service. The other state-specific URLs in this repo
(`dep.wv.gov`, `apps.wv.gov`, `workforcewv.org`, `tagis.dep.wv.gov`) appear only
inside print statements that tell a human where to look; nothing downloads them.
So a state without a known parcel service is still worth running. It loses the
parcel acreage enrichment and the adjacent-land step, and `readiness()` says so
by name rather than failing opaquely at step 11.

NEVER GUESS A FIELD
-------------------
A state declared here with `fips: null` is a state whose FIPS code nobody has
looked up yet. Leave it null. A plausible-looking wrong FIPS silently screens
the wrong state's facilities and every downstream number inherits it, which is
far worse than a config the validator refuses to run.
"""
from __future__ import annotations

import json
import os
import re

REGISTRY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'registry')

# Needed before the pipeline can screen this state at all.
REQUIRED = ('abbr', 'name', 'nhd_name', 'fips', 'bbox', 'state_score')

# Needed only by the parcel-acreage enrichment and the adjacent-land step.
# A state missing this is PARTIAL, not broken.
PARCEL_KEYS = ('url', 'owner', 'acres', 'landuse')

READY_FULL = 'full'        # every required field, plus a parcel service
READY_PARTIAL = 'partial'  # every required field, no parcel service
READY_DECLARED = 'declared'  # named, but not yet runnable


class StateConfigError(ValueError):
    """Raised when a state config is malformed, as opposed to merely incomplete.

    Incomplete is an expected state and is reported by readiness(). Malformed
    means the file itself is wrong and no reading of it is safe.
    """


def derive_nhd_name(name):
    """Derive the NHD S3 filename form of a state name.

    `download_data.py` builds `NHD_H_{nhd_name}_State_Shape.zip`, and the one
    known-good case is "West Virginia" -> "West_Virginia": the display name with
    spaces as underscores. test_nhd_derivation_reproduces_wv pins that.

    DERIVED IS NOT VERIFIED. The rule is unconfirmed for any state but WV, so a
    derived value is marked in the config it is loaded into. It is also cheap to
    be wrong about: the URL either resolves on the first download or 404s
    immediately, which is a loud, first-step failure rather than a silent one.
    That is why this is derived rather than left unfilled, and why it is labelled
    rather than written into the registry files as though someone looked it up.
    """
    if not name:
        return None
    return name.strip().replace(' ', '_')


def _check_bbox(abbr, bbox, problems):
    if bbox is None:
        return
    if not isinstance(bbox, list) or len(bbox) != 4:
        problems.append(f"{abbr}: bbox must be [west, south, east, north], got {bbox!r}")
        return
    if not all(isinstance(v, (int, float)) for v in bbox):
        problems.append(f"{abbr}: bbox values must be numbers, got {bbox!r}")
        return
    w, s, e, n = bbox
    if not (-180 <= w < e <= -60):
        problems.append(f"{abbr}: bbox longitudes must run west to east inside the US, got {w} to {e}")
    if not (15 <= s < n <= 72):
        problems.append(f"{abbr}: bbox latitudes must run south to north inside the US, got {s} to {n}")


def validate(cfg):
    """Return a list of problems with one state config. Empty means well formed.

    This checks SHAPE, not completeness: a field that is present must be the
    right type and inside a sane range, and a field that is null is reported by
    readiness() instead. The two are separate on purpose, because "nobody has
    looked this up yet" and "this value is wrong" need different responses.
    """
    problems = []
    if not isinstance(cfg, dict):
        return [f"config is {type(cfg).__name__}, expected an object"]

    abbr = cfg.get('abbr')
    if not isinstance(abbr, str) or not re.fullmatch(r'[A-Z]{2}', abbr or ''):
        problems.append(f"abbr must be two uppercase letters, got {abbr!r}")
        abbr = abbr or '??'

    for key in ('name', 'nhd_name'):
        v = cfg.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            problems.append(f"{abbr}: {key} must be a non-empty string or null, got {v!r}")

    fips = cfg.get('fips')
    if fips is not None and (not isinstance(fips, str) or not re.fullmatch(r'\d{2}', fips)):
        problems.append(
            f"{abbr}: fips must be the two-digit state code AS A STRING "
            f"(leading zeros matter, '01' is not 1), got {fips!r}")

    _check_bbox(abbr, cfg.get('bbox'), problems)

    score = cfg.get('state_score')
    if score is not None and not isinstance(score, (int, float)):
        problems.append(f"{abbr}: state_score must be a number or null, got {score!r}")

    parcels = cfg.get('parcels')
    if parcels is not None:
        if not isinstance(parcels, dict):
            problems.append(f"{abbr}: parcels must be an object or null, got {type(parcels).__name__}")
        else:
            svc = parcels.get('statewide_service')
            if svc is not None:
                if not isinstance(svc, dict):
                    problems.append(f"{abbr}: parcels.statewide_service must be an object or null")
                else:
                    for k in PARCEL_KEYS:
                        if k not in svc:
                            problems.append(f"{abbr}: parcels.statewide_service missing key {k!r}")
                    if svc.get('url') is not None and not str(svc['url']).startswith('https://'):
                        problems.append(f"{abbr}: parcels.statewide_service.url must be https")
            ovr = parcels.get('county_overrides')
            if ovr is not None and not isinstance(ovr, dict):
                problems.append(f"{abbr}: parcels.county_overrides must be an object or null")

    return problems


def missing_required(cfg):
    """Return the required fields this state has not had filled in yet."""
    return [k for k in REQUIRED if cfg.get(k) in (None, '', [])]


def has_parcel_service(cfg):
    svc = (cfg.get('parcels') or {}).get('statewide_service')
    return bool(svc and svc.get('url'))


def readiness(cfg):
    """Return (level, reasons) for how far this state can be run.

    full     -- screen it end to end.
    partial  -- screen it, but the parcel-acreage enrichment and the
                adjacent-land step have no source and will be skipped.
    declared -- named only; required fields are still unfilled. The reasons
                name each one, so the remaining work is explicit rather than
                discovered when a run fails.
    """
    # Build the derived-field disclosure FIRST. An earlier version returned on
    # the missing-field branch before reaching this, so a state that was both
    # incomplete and carrying a derived value disclosed only the incompleteness.
    # Every level discloses derivation now.
    notes = []
    for field in cfg.get('derived', []):
        notes.append(f"{field} is derived by rule, not looked up, and is "
                     f"unverified for this state")

    missing = missing_required(cfg)
    if missing:
        return READY_DECLARED, (
            [f"required field not filled in: {k}" for k in missing] + notes)
    if not has_parcel_service(cfg):
        return READY_PARTIAL, notes + [
            "no statewide parcel service, so parcel acreage enrichment and the "
            "adjacent-land step are skipped for this state"]
    return READY_FULL, notes


def load(abbr):
    """Load one state config by its two-letter code. Raises if malformed."""
    abbr = abbr.upper()
    path = os.path.join(REGISTRY_DIR, f"{abbr.lower()}.json")
    if not os.path.exists(path):
        known = ', '.join(sorted(all_codes())) or 'none'
        raise StateConfigError(f"no state config for {abbr!r} at {path}. Known: {known}")
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    problems = validate(cfg)
    if problems:
        raise StateConfigError(f"{path} is malformed:\n  " + "\n  ".join(problems))
    if cfg.get('abbr') != abbr:
        raise StateConfigError(
            f"{path} declares abbr {cfg.get('abbr')!r} but is filed as {abbr!r}")

    # Fill what can be derived, and record that it was derived. The registry
    # files keep a null so nothing in them reads as looked-up when it was not.
    cfg.setdefault('derived', [])
    if cfg.get('nhd_name') is None and cfg.get('name'):
        cfg['nhd_name'] = derive_nhd_name(cfg['name'])
        cfg['derived'] = sorted(set(cfg['derived']) | {'nhd_name'})
    return cfg


def all_codes():
    if not os.path.isdir(REGISTRY_DIR):
        return []
    return [fn[:-5].upper() for fn in os.listdir(REGISTRY_DIR) if fn.endswith('.json')]


def load_all():
    """Load every state config. Raises on the first malformed one."""
    return {c: load(c) for c in sorted(all_codes())}
