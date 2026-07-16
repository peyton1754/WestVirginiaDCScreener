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

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
HEADERS = {"User-Agent": "DataCenterScreener/1.0"}

# Statewide WV parcel layer (same as enrich_columns.py) — covers all 55
# counties in one composite service, so no county-by-county overrides are
# needed (unlike Tennessee, which this script was originally written for).
STATEWIDE_SVC = {
    "url": "https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels/MapServer/0/query",
    "owner": "FullOwnerName", "acres": "Acres_C", "landuse": None,
}

COUNTY_OVERRIDES = {}

# Vacancy/undeveloped land use keywords (case-insensitive)
VACANT_KEYWORDS = [
    "vacant", "undeveloped", "unused", "empty", "open land", "forest",
    "timber", "agricultural", "farm", "woodland", "row crop", "pasture",
    "idle", "unimproved",
]

def is_likely_vacant(owner: str, landuse: str) -> bool:
    text = f"{owner} {landuse}".lower()
    return any(kw in text for kw in VACANT_KEYWORDS)


def query_adjacent(svc, lon, lat, buffer_m=600):
    out_fields = ",".join(f for f in [
        svc.get("owner"), svc.get("acres"), svc.get("landuse")
    ] if f)
    # Convert buffer_m to approximate degree offsets (600m ≈ 0.0054 lat, 0.0070 lon at WV latitude)
    pad_lat = buffer_m / 111_000
    pad_lon = buffer_m / (111_000 * 0.78)  # cos(39°) ≈ 0.78
    bbox = f"{lon-pad_lon},{lat-pad_lat},{lon+pad_lon},{lat+pad_lat}"
    try:
        r = requests.get(svc["url"], params={
            "geometry": bbox,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
        }, headers=HEADERS, timeout=20, verify=False)
        return r.json().get("features", [])
    except Exception as e:
        print(f"    [WARN] {e}")
        return []


def summarize_adjacent(site_name, county, lat, lon):
    county_key = county.upper().replace(" COUNTY", "")

    svc = STATEWIDE_SVC
    feats = query_adjacent(svc, lon, lat, buffer_m=600)
    if not feats and county_key in COUNTY_OVERRIDES:
        svc = COUNTY_OVERRIDES[county_key]
        feats = query_adjacent(svc, lon, lat, buffer_m=600)
    if not feats:
        return {"status": "no_features", "note": f"No parcels returned within 600m for {county} (statewide layer + county override both empty/unavailable)"}

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

    # Sort by size desc, skip the site parcel itself (largest in buffer = likely the site)
    parcels.sort(key=lambda x: x["acres"], reverse=True)
    site_parcel = parcels[0] if parcels else None
    neighbors = parcels[1:] if len(parcels) > 1 else []

    total_neighbor_acres = sum(p["acres"] for p in neighbors)
    vacant_neighbors = [p for p in neighbors if is_likely_vacant(p["owner"], p["landuse"])]
    vacant_acres = sum(p["acres"] for p in vacant_neighbors)

    # Check if site owner also owns adjacent parcels
    site_owner = site_parcel["owner"] if site_parcel else ""
    same_owner_neighbors = [
        p for p in neighbors
        if site_owner and site_owner.lower() in p["owner"].lower()
        and p["acres"] > 1
    ]
    same_owner_acres = sum(p["acres"] for p in same_owner_neighbors)

    return {
        "status": "ok",
        "site_acres": site_parcel["acres"] if site_parcel else 0,
        "site_owner": site_owner,
        "n_neighbors": len(neighbors),
        "total_neighbor_acres": round(total_neighbor_acres, 1),
        "vacant_neighbors": len(vacant_neighbors),
        "vacant_acres": round(vacant_acres, 1),
        "same_owner_adjacent": len(same_owner_neighbors),
        "same_owner_acres": round(same_owner_acres, 1),
        "neighbor_detail": neighbors[:8],  # top 8 by size
    }


def expansion_verdict(result):
    if result["status"] != "ok":
        return "UNKNOWN — no GIS data"
    vacant = result["vacant_acres"]
    same = result["same_owner_acres"]
    total_potential = result["site_acres"] + vacant + same
    if total_potential >= 100:
        return f"STRONG — up to {total_potential:.0f} acres possible (site + adjacent vacant/same-owner land)"
    elif total_potential >= 50:
        return f"MODERATE — up to {total_potential:.0f} acres possible"
    elif result["n_neighbors"] > 0:
        return f"LIMITED — {result['n_neighbors']} neighbors totaling {result['total_neighbor_acres']:.0f} acres but little vacant land identified"
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

    result = summarize_adjacent(name, county, lat, lon)

    if result["status"] == "ok":
        print(f"  Site parcel:    {result['site_acres']:.1f} acres (owner: {result['site_owner']})")
        print(f"  Neighbors:      {result['n_neighbors']} parcels within 600m  ({result['total_neighbor_acres']:.1f} total acres)")
        print(f"  Vacant nearby:  {result['vacant_neighbors']} parcels  ({result['vacant_acres']:.1f} acres)")
        print(f"  Same owner adj: {result['same_owner_adjacent']} parcels ({result['same_owner_acres']:.1f} acres)")
        print(f"\n  Top neighboring parcels:")
        for p in result["neighbor_detail"][:5]:
            tag = " ← VACANT" if is_likely_vacant(p["owner"], p["landuse"]) else ""
            lu = f"  [{p['landuse']}]" if p["landuse"] else ""
            print(f"    {p['acres']:>7.1f} ac  {p['owner'][:45]}{lu[:30]}{tag}")
    else:
        print(f"  {result['note']}")

    verdict = expansion_verdict(result)
    print(f"\n  Expansion potential: {verdict}")

    rows.append({
        "rank": int(site["rank"]),
        "Plant_Name": name,
        "City": site["City"],
        "County": county,
        "site_acres": own_acres,
        "site_owner": result.get("site_owner", ""),
        "vacant_adjacent_acres": result.get("vacant_acres", 0),
        "same_owner_adjacent_acres": result.get("same_owner_acres", 0),
        "total_potential_acres": own_acres + result.get("vacant_acres", 0) + result.get("same_owner_acres", 0),
        "expansion_verdict": verdict,
    })

cols = ["rank","Plant_Name","City","County","site_acres","site_owner",
        "vacant_adjacent_acres","same_owner_adjacent_acres","total_potential_acres","expansion_verdict"]
out = pd.DataFrame(rows, columns=cols)
out_path = ROOT / "outputs" / "csv" / "adjacent_land_25_50ac.csv"
out.to_csv(out_path, index=False)
print(f"\n\nSaved: {out_path}")
if out.empty:
    print("No sites in the 25-50 acre range — nothing to report.")
else:
    print(out[["rank","Plant_Name","site_acres","vacant_adjacent_acres","total_potential_acres","expansion_verdict"]].to_string(index=False))
