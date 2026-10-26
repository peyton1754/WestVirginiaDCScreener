"""
check_adjacent_land.py
For each site in the 25-50 acre range, queries county GIS parcel services
to identify neighboring parcels and flag expansion potential.

Outputs a summary of adjacent land availability per site.
"""

import requests
import warnings
import pandas as pd
from pathlib import Path
from states import resolve
import source_probe

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
HEADERS = {"User-Agent": "DataCenterScreener/1.0"}

# Statewide WV parcel layer (same as enrich_columns.py) — covers all 55
# counties in one composite service, so no county-by-county overrides are
# needed (unlike Tennessee, which this script was originally written for).
_STATE = resolve.single_selected("check_adjacent_land.py")
STATEWIDE_SVC = resolve.statewide_service(_STATE)

COUNTY_OVERRIDES = resolve.county_overrides(_STATE)

# Vacancy/undeveloped land use keywords (case-insensitive)
VACANT_KEYWORDS = [
    "vacant", "undeveloped", "unused", "empty", "open land", "forest",
    "timber", "agricultural", "farm", "woodland", "row crop", "pasture",
    "idle", "unimproved",
]

def is_likely_vacant(owner: str, landuse: str) -> bool:
    text = f"{owner} {landuse}".lower()
    return any(kw in text for kw in VACANT_KEYWORDS)


#: Whether this state's parcel layer carries a land-use field at all.
#: wv.json sets parcels.statewide_service.landuse to null because the WVGIS
#: layer has none: refresh run 4 (id 40276191635) printed all 21 of its field
#: names and no land-use field is among them. So on WV, is_likely_vacant()
#: matches OWNER NAMES only -- a parcel reads as vacant because its owner is
#: called something containing "FARM" or "TIMBER". That may be the best
#: signal available; it is not a land-use classification, and until now
#: nothing in the output distinguished the two.
HAS_LANDUSE_FIELD = bool(STATEWIDE_SVC.get("landuse"))
VACANCY_BASIS = ("owner_name_and_landuse" if HAS_LANDUSE_FIELD
                 else "owner_name_only")


#: How close a returned parcel's acreage must be to the candidate row's own
#: acreage to be accepted as the site's parcel. Generous, because the two
#: figures can come from different sources: parcel_acres is a point query
#: against this same layer and matches to four decimals, while osm_acres is a
#: digitised polygon and will not.
SITE_MATCH_FRACTION = 0.5
SITE_MATCH_FLOOR_ACRES = 10.0


def finite_acres(value):
    """`value` as a positive float, or None.

    One guard for both the matcher and the base. NaN is the case that
    matters: `best_acres` is parcel_acres combined with osm_acres and is NaN
    when a site has neither, and NaN defeats every ordinary bound because
    all its comparisons are False. `own <= 0` does not catch it,
    `max(10.0, 0.5 * nan)` returns 10.0, and `min(..., key=nan)` returns the
    first element. Together those made a NaN base accept whichever parcel
    came back first and report it as the site.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float('inf'), float('-inf')) or v <= 0:
        return None
    return v


def identify_site_parcel(parcels, own_acres):
    """Which of the returned parcels is the site's own. Returns (index, note).

    Matched by ACREAGE against the candidate row, not by being the largest.
    "Largest in the buffer" is wrong whenever a neighbour is bigger, and it
    was wrong in one of the two rows on file: AURORA ACQUISITION's box
    contains a 163-acre neighbour, so a 28-acre site was credited with the
    neighbour's acreage and its own parcel was counted as adjacent land.

    Returns (None, reason) when nothing matches, rather than falling back to
    the largest. A wrong match is the expensive direction: the DTE Smith
    Branch parcel in top_candidates_wv.csv is 23,476 acres of coal
    landholding, and treating one of those as "the site" turns a 28-acre
    candidate into STRONG expansion potential. A missing match costs one
    site's verdict and says so.
    """
    own = finite_acres(own_acres)
    if own is None:
        return None, (f"the candidate row carries no usable acreage to match "
                      f"against (got {own_acres!r})")
    if not parcels:
        return None, "no parcels were returned to match against"

    tolerance = max(SITE_MATCH_FLOOR_ACRES, SITE_MATCH_FRACTION * own)
    best = min(range(len(parcels)),
               key=lambda i: abs(parcels[i]["acres"] - own))
    gap = abs(parcels[best]["acres"] - own)
    if gap > tolerance:
        return None, (
            f"no returned parcel is within {tolerance:.0f} acres of the "
            f"candidate's {own:.1f}; closest was {parcels[best]['acres']:.1f}")
    return best, f"matched to within {gap:.2f} acres of the candidate's {own:.1f}"


#: Tally across every adjacent-land query, reported at the end of the
#: run. Without it a site with no neighbours and a site whose query
#: failed both read as "no_features".
ADJACENT_OUTCOMES = source_probe.OutcomeCounter("WVGIS adjacent-parcel queries")


def query_adjacent(svc, lon, lat, buffer_m=600):
    out_fields = ",".join(f for f in [
        svc.get("owner"), svc.get("acres"), svc.get("landuse")
    ] if f)
    # Convert buffer_m to approximate degree offsets (600m ≈ 0.0054 lat, 0.0070 lon at WV latitude)
    pad_lat = buffer_m / 111_000
    pad_lon = buffer_m / (111_000 * 0.78)  # cos(39°) ≈ 0.78
    bbox = f"{lon-pad_lon},{lat-pad_lat},{lon+pad_lon},{lat+pad_lat}"
    status, detail, feats, tries = source_probe.fetch(
        lambda: requests.get(svc["url"], params={
            "geometry": bbox,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
        }, headers=HEADERS, timeout=20, verify=False),
        attempts=3)
    ADJACENT_OUTCOMES.record(status, detail, tries)
    if status in source_probe.FAILURES:
        print(f"    [FAIL] parcel query {status}: {detail}")
    # Returns the STATUS as well as the features. Returning a bare
    # list left the caller with [] for a failure and [] for a genuine
    # empty, so it reported both as "no parcels within 600m" however
    # carefully this function tallied them.
    return status, feats


def summarize_adjacent(site_name, county, lat, lon, own_acres):
    """`own_acres` is the base for every acreage figure this returns.

    It used to be absent, and the caller added it to the vacant and
    same-owner acreage for the CSV while expansion_verdict() added the
    largest parcel in the buffer instead. The same row then carried two
    different totals.
    """
    county_key = county.upper().replace(" COUNTY", "")

    svc = STATEWIDE_SVC
    status, feats = query_adjacent(svc, lon, lat, buffer_m=600)
    if not feats and county_key in COUNTY_OVERRIDES:
        svc = COUNTY_OVERRIDES[county_key]
        status, feats = query_adjacent(svc, lon, lat, buffer_m=600)
    if status in source_probe.FAILURES:
        # NOT "no_features". The service never answered, so nothing is
        # known about this site's neighbours either way.
        return {"status": "query_failed", "query_status": status,
                "note": f"Parcel service FAILED ({status}) for {county}. "
                        f"Nothing is known about adjacent land here; this is "
                        f"not a finding that the site has no neighbours."}
    if not feats:
        # The service ANSWERED. That is a finding, and it has a total: the
        # site's own acreage, with nothing adjacent to add. base_acres is
        # carried here so total_potential_acres() can say so rather than
        # returning None and losing a real figure.
        return {"status": "no_features",
                "base_acres": finite_acres(own_acres),
                "vacant_acres": 0.0, "same_owner_acres": 0.0,
                "n_neighbors": 0, "total_neighbor_acres": 0.0,
                "vacancy_basis": VACANCY_BASIS,
                "note": f"Service answered: no parcels within 600m for {county}"}

    owner_field = svc.get("owner", "")
    acres_field = svc.get("acres", "")
    landuse_field = svc.get("landuse") or ""

    parcels = []
    for f in feats:
        a = f["attributes"]
        raw_acres = a.get(acres_field, 0) or 0
        try:
            acres = float(raw_acres)
        except (TypeError, ValueError):
            acres = 0.0
        owner = str(a.get(owner_field, "") or "").strip()
        landuse = str(a.get(landuse_field, "") or "").strip() if landuse_field else ""
        parcels.append({"owner": owner, "acres": acres, "landuse": landuse})

    parcels.sort(key=lambda x: x["acres"], reverse=True)
    site_idx, match_note = identify_site_parcel(parcels, own_acres)
    site_parcel = parcels[site_idx] if site_idx is not None else None
    neighbors = [p for i, p in enumerate(parcels) if i != site_idx]

    total_neighbor_acres = sum(p["acres"] for p in neighbors)
    vacant_neighbors = [p for p in neighbors if is_likely_vacant(p["owner"], p["landuse"])]
    vacant_acres = sum(p["acres"] for p in vacant_neighbors)

    # Check if site owner also owns adjacent parcels. With no site parcel
    # identified there is no owner to compare against, so this is left empty
    # rather than compared against whichever parcel happened to be largest.
    site_owner = site_parcel["owner"] if site_parcel else ""
    same_owner_neighbors = [
        p for p in neighbors
        if site_owner and site_owner.lower() in p["owner"].lower()
        and p["acres"] > 1
    ]
    same_owner_acres = sum(p["acres"] for p in same_owner_neighbors)

    return {
        "status": "ok",
        # THE one base. Every acreage figure below adds to this, and the
        # verdict and the CSV now read it from the same place. None rather
        # than NaN when the candidate row has no acreage, so
        # total_potential_acres() returns None instead of a NaN that formats
        # as "nan" in the CSV and sums to nan in a spreadsheet.
        "base_acres": finite_acres(own_acres),
        "site_parcel_identified": site_idx is not None,
        "site_parcel_match_note": match_note,
        "site_parcel_acres": site_parcel["acres"] if site_parcel else None,
        "site_owner": site_owner,
        "vacancy_basis": VACANCY_BASIS,
        "n_neighbors": len(neighbors),
        "total_neighbor_acres": round(total_neighbor_acres, 1),
        "vacant_neighbors": len(vacant_neighbors),
        "vacant_acres": round(vacant_acres, 1),
        "same_owner_adjacent": len(same_owner_neighbors),
        "same_owner_acres": round(same_owner_acres, 1),
        "neighbor_detail": neighbors[:8],  # top 8 by size
    }


def total_potential_acres(result):
    """The one place this sum is computed.

    It existed in two places with two different bases, and the two disagreed
    by 135 acres on the first row of the shipped CSV. Returns None for a
    result that measured nothing, because a number there would be a
    fabricated measurement.
    """
    # "query_failed" is the only status with no total. "no_features" is an
    # ANSWER -- the service said there is nothing adjacent -- so its total is
    # the site's own acreage.
    if result.get("status") not in ("ok", "no_features"):
        return None
    base = result.get("base_acres")
    if base is None:
        return None
    return base + result.get("vacant_acres", 0.0) + result.get("same_owner_acres", 0.0)


def expansion_verdict(result):
    if result["status"] == "query_failed":
        return (f"UNMEASURED — parcel service failed "
                f"({result.get('query_status', 'unknown')}); not a finding")
    if result["status"] != "ok":
        return "UNKNOWN — no GIS data"
    # From total_potential_acres(), the same function the CSV writes, so the
    # verdict and the cell beside it can no longer disagree.
    total_potential = total_potential_acres(result)
    # Named in the verdict, because "vacant" here is a keyword match on the
    # owner's NAME wherever the layer has no land-use field, and a reader
    # cannot tell that from the number.
    basis = ("" if result.get("vacancy_basis") == "owner_name_and_landuse"
             else " [vacancy inferred from owner names only]")
    if not result.get("site_parcel_identified", True):
        # The site's own parcel could not be matched, so it is still inside
        # the neighbour set and this sum may double-count it.
        basis += (f" [site parcel unmatched: "
                  f"{result.get('site_parcel_match_note', 'no reason recorded')}]")
    if total_potential >= 100:
        return (f"STRONG — up to {total_potential:.0f} acres possible "
                f"(site + adjacent vacant/same-owner land){basis}")
    elif total_potential >= 50:
        return f"MODERATE — up to {total_potential:.0f} acres possible{basis}"
    elif result["n_neighbors"] > 0:
        return (f"LIMITED — {result['n_neighbors']} neighbors totaling "
                f"{result['total_neighbor_acres']:.0f} acres but little vacant "
                f"land identified{basis}")
    else:
        return "UNKNOWN — no neighboring parcels returned"


# ── Main ─────────────────────────────────────────────────────────────────────

df = pd.read_csv(ROOT / "outputs" / "csv" / "top_candidates_wv.csv")
df["best_acres"] = df["parcel_acres"].combine_first(df["osm_acres"])
mid_sites = df[(df["best_acres"] >= 25) & (df["best_acres"] < 50)].copy()

print("=" * 70)
print("Adjacent Land Availability — Sites 25-50 Acres")
print("=" * 70)

rows = []
for _, site in mid_sites.iterrows():
    name = site["Plant_Name"]
    county = site["County"]
    lat, lon = site["Latitude"], site["Longitude"]
    own_acres = site["best_acres"]

    print(f"\n{'─'*70}")
    print(f"  {name}")
    print(f"  {site['City']}, {county} County  |  {own_acres:.1f} acres  |  Rank #{int(site['rank'])}")

    result = summarize_adjacent(name, county, lat, lon, own_acres)

    if result["status"] == "ok":
        if result["site_parcel_identified"]:
            print(f"  Site parcel:    {result['site_parcel_acres']:.1f} acres "
                  f"(owner: {result['site_owner']}; {result['site_parcel_match_note']})")
        else:
            # Named, not guessed. Falling back to the largest parcel is what
            # credited a 28-acre site with a 163-acre neighbour's acreage.
            print(f"  Site parcel:    NOT IDENTIFIED - "
                  f"{result['site_parcel_match_note']}. Every returned parcel "
                  f"is therefore counted as a neighbour, so the figures below "
                  f"may include the site's own land.")
        print(f"  Neighbors:      {result['n_neighbors']} parcels within 600m  ({result['total_neighbor_acres']:.1f} total acres)")
        print(f"  Vacant nearby:  {result['vacant_neighbors']} parcels  ({result['vacant_acres']:.1f} acres)"
              + ("" if result["vacancy_basis"] == "owner_name_and_landuse"
                 else "  [owner-name keywords only; this layer has no land-use field]"))
        print(f"  Same owner adj: {result['same_owner_adjacent']} parcels ({result['same_owner_acres']:.1f} acres)")
        print(f"\n  Top neighboring parcels:")
        for p in result["neighbor_detail"][:5]:
            tag = " ← VACANT" if is_likely_vacant(p["owner"], p["landuse"]) else ""
            lu = f"  [{p['landuse']}]" if p["landuse"] else ""
            print(f"    {p['acres']:>7.1f} ac  {p['owner'][:45]}{lu[:30]}{tag}")
    else:
        print(f"  {result['note']}")

    verdict = expansion_verdict(result)
    _failed = result.get("status") == "query_failed"
    print(f"\n  Expansion potential: {verdict}")

    rows.append({
        "rank": int(site["rank"]),
        "Plant_Name": name,
        "City": site["City"],
        "County": county,
        "site_acres": own_acres,
        "site_owner": result.get("site_owner", ""),
        # New columns, and the reason they exist. Before these, a reader of
        # this CSV could not tell a keyword guess about an owner's name from
        # a land-use classification, nor know that the site's own parcel had
        # not been found and might be inside the neighbour figures.
        "vacancy_basis": result.get("vacancy_basis", ""),
        "site_parcel_identified": result.get("site_parcel_identified"),
        "site_parcel_acres": result.get("site_parcel_acres"),
        # BLANK, not 0, when the service failed. A zero here is a
        # measurement: it says this site has no vacant land next to it.
        # Writing one for a query that never answered put a fabricated
        # measurement into the CSV, and anyone summing or sorting the
        # column would have treated an outage as zero vacancy.
        "vacant_adjacent_acres": None if _failed else result.get("vacant_acres", 0),
        "same_owner_adjacent_acres": None if _failed else result.get("same_owner_acres", 0),
        # From the shared function, which the verdict also calls. This cell
        # used to add own_acres while expansion_verdict() added the largest
        # parcel in the buffer, and on row 5 of the shipped CSV the two
        # disagreed by 135 acres: 166 here against "up to 301" beside it.
        "total_potential_acres": total_potential_acres(result),
        "expansion_verdict": verdict,
    })

cols = ["rank","Plant_Name","City","County","site_acres","site_owner",
        "site_parcel_identified","site_parcel_acres","vacancy_basis",
        "vacant_adjacent_acres","same_owner_adjacent_acres","total_potential_acres","expansion_verdict"]
out = pd.DataFrame(rows, columns=cols)
out_path = ROOT / "outputs" / "csv" / "adjacent_land_25_50ac.csv"
out.to_csv(out_path, index=False)
print(f"\n\nSaved: {out_path}")
ADJACENT_OUTCOMES.report()
if out.empty:
    if ADJACENT_OUTCOMES.total == 0:
        print("No sites in the 25-50 acre range, so no parcel query ran. "
              "This says nothing about adjacent land; it says no site "
              "reached the size filter.")
    else:
        print("No sites in the 25-50 acre range — nothing to report.")
else:
    print(out[["rank","Plant_Name","site_acres","vacant_adjacent_acres","total_potential_acres","expansion_verdict"]].to_string(index=False))
