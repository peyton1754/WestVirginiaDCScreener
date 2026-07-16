"""
fetch_westvirginia_parcels.py
Fetches owner and acreage data for West Virginia candidates from available sources:

  1. OSM industrial polygon area — Overpass API queries for landuse=industrial
     polygons near each candidate, computes area in acres from polygon geometry
  2. FRS operator name — uses PRIMARY_NAME from FRS as last-known operator
  3. Nominatim reverse geocode — gets address details for each candidate
  4. TRI owner name — TRI facility table includes parent company names

Output: updates candidates_enriched_wv.gpkg with current_owner, parcel_acres, osm_acres

Run after enrich_candidates_al.py and enrich_retirement_al.py, before score_and_export_al.py.
"""

import math
import time
import warnings
import requests
import numpy as np
import pandas as pd
import geopandas as gpd
from pathlib import Path
from shapely.geometry import Polygon
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

ROOT     = Path(__file__).parent
PROC_DIR = ROOT / "data" / "processed"
AL_RAW   = ROOT / "data" / "westvirginia" / "raw"
FRS_DIR  = ROOT / "data" / "raw" / "frs"
STATE    = "wv"

HEADERS = {"User-Agent": "DataCenterScreener/1.0 arthur.b.fok@gmail.com"}

print("=" * 60)
print("West Virginia Parcel & Ownership Lookup")
print("=" * 60)

# Load candidates
src = PROC_DIR / f"candidates_enriched_{STATE}.gpkg"
cands = gpd.read_file(src)
cands_wgs = cands.to_crs("EPSG:4326")
print(f"\nLoaded {len(cands)} candidates from {src.name}")


# ===================================================================
# 1. FRS operator name (from original FRS facility file)
# ===================================================================
print("\n" + "=" * 60)
print("1. FRS Operator Name")
print("=" * 60)

frs_path = FRS_DIR / "WV_FACILITY_FILE.CSV"
if frs_path.exists():
    frs = pd.read_csv(frs_path, low_memory=False, encoding="latin-1")
    frs.columns = [c.strip().upper() for c in frs.columns]
    frs_lookup = {}
    for _, row in frs.iterrows():
        rid = str(row.get("REGISTRY_ID", "")).strip()
        name = str(row.get("PRIMARY_NAME", "")).strip()
        if rid and name:
            frs_lookup[rid] = name

    n_frs = 0
    for i in range(len(cands)):
        sid = str(cands.iloc[i].get("site_id", "")).replace("FRS_", "")
        if sid in frs_lookup:
            cands.at[cands.index[i], "current_owner"] = frs_lookup[sid]
            cands.at[cands.index[i], "owner_source"] = "FRS_PRIMARY_NAME"
            n_frs += 1
    # EIA plants: use Plant_Name as owner
    for i in range(len(cands)):
        if str(cands.iloc[i].get("source", "")) == "EIA860" and not cands.iloc[i].get("current_owner"):
            cands.at[cands.index[i], "current_owner"] = str(cands.iloc[i]["Plant_Name"])
            cands.at[cands.index[i], "owner_source"] = "EIA860_PLANT_NAME"
    print(f"  Set owner for {n_frs} FRS sites + EIA plants")
else:
    print("  [skip] FRS file not found")


# ===================================================================
# 2. TRI parent company name (more current than FRS for some sites)
# ===================================================================
print("\n" + "=" * 60)
print("2. TRI Parent Company Override")
print("=" * 60)

print("  Fetching TRI facility table for parent company names …")
try:
    offset, tri_rows = 0, []
    while True:
        url = f"https://data.epa.gov/efservice/tri_facility/state_abbr/WV/rows/{offset}:{offset+10000}/json"
        r = requests.get(url, headers=HEADERS, timeout=120)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        tri_rows.extend(data)
        if len(data) < 10000:
            break
        offset += 10000

    if tri_rows:
        tri_df = pd.DataFrame(tri_rows)
        tri_df["_name"] = tri_df["facility_name"].astype(str).str.upper().str.strip()
        tri_df["_city"] = tri_df["city_name"].astype(str).str.upper().str.strip()
        # parent_co_name is the corporate parent
        tri_parent = {}
        for _, row in tri_df.iterrows():
            key = (row["_name"], row["_city"])
            parent = str(row.get("parent_co_name", "")).strip()
            if parent and parent.upper() not in ("NAN", "NA", "N/A", "NONE", "NULL"):
                tri_parent[key] = parent

        n_parent = 0
        from difflib import get_close_matches
        tri_names = list(set(k[0] for k in tri_parent))
        for i in range(len(cands)):
            cand_name = str(cands.iloc[i]["Plant_Name"]).upper().strip()
            cand_city = str(cands.iloc[i].get("City", "")).upper().strip()

            parent = None
            if (cand_name, cand_city) in tri_parent:
                parent = tri_parent[(cand_name, cand_city)]
            else:
                city_names = [k[0] for k in tri_parent if k[1] == cand_city]
                if city_names:
                    m = get_close_matches(cand_name, city_names, n=1, cutoff=0.50)
                    if m:
                        parent = tri_parent.get((m[0], cand_city))

            if parent:
                current = str(cands.iloc[i].get("current_owner", "")).strip()
                # Only override if TRI parent is different and more informative
                if parent.upper() != cand_name and parent.upper() != current.upper():
                    cands.at[cands.index[i], "current_owner"] = f"{parent} (TRI parent)"
                    cands.at[cands.index[i], "owner_source"] = "TRI_PARENT_CO"
                    n_parent += 1

        print(f"  TRI parent company overrides: {n_parent}")
except Exception as e:
    print(f"  [WARN] TRI parent fetch failed: {e}")


# ===================================================================
# 3. County ArcGIS Parcel Services — owner + acreage
# ===================================================================
print("\n" + "=" * 60)
print("3. County ArcGIS Parcel Services")
print("=" * 60)

import warnings as _w
_w.filterwarnings("ignore")

# Unlike Alabama/Tennessee, West Virginia has a single statewide parcel
# service (WVGIS WV_Parcels — see fetch_parcels_wv.py) that already covers
# every county, so no county-by-county fallback list is needed here.
COUNTY_PARCEL_SERVICES = {}

def query_county_parcel(url, lon, lat, owner_field, acres_field, value_field="", imp_field=""):
    out_fields = ",".join(f for f in [owner_field, acres_field, value_field, imp_field, "Shape__Area"] if f)
    try:
        r = requests.get(url, params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "distance": "200",
            "units": "esriSRUnit_Meter",
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
        }, headers=HEADERS, timeout=15, verify=False)
        if r.status_code != 200:
            return None, None, None, None
        feats = r.json().get("features", [])
        if not feats:
            return None, None, None, None

        # Take the largest parcel (by acres or Shape__Area)
        best = None
        best_area = 0
        for f in feats:
            a = f["attributes"]
            area = float(a.get(acres_field, 0) or 0) if acres_field else 0
            if not area:
                shape_area = float(a.get("Shape__Area", 0) or 0)
                area = shape_area / 4046.86 if shape_area > 0 else 0
            if area > best_area:
                best_area = area
                best = a

        if best:
            owner = str(best.get(owner_field, "")).strip() if owner_field else ""
            acres = best_area if best_area > 0 else None
            imp_val = float(best.get(imp_field, 0) or 0) if imp_field else None
            return owner, acres, imp_val, len(feats)
    except Exception:
        pass
    return None, None, None, None

# Query each candidate against its county's parcel service
n_county = 0
n_county_owner = 0
n_county_acres = 0

for i in range(len(cands)):
    county = str(cands.iloc[i].get("County", "")).upper().strip()
    county = county.replace(" COUNTY", "")
    if county not in COUNTY_PARCEL_SERVICES:
        continue

    svc = COUNTY_PARCEL_SERVICES[county]
    pt = cands_wgs.geometry.iloc[i]
    owner, acres, imp_val, n_parcels = query_county_parcel(
        svc["url"], pt.x, pt.y,
        svc["owner_field"], svc["acres_field"],
        svc.get("value_field", ""), svc.get("imp_field", ""),
    )

    if owner:
        cands.at[cands.index[i], "current_owner"] = owner
        cands.at[cands.index[i], "owner_source"] = f"COUNTY_GIS_{county}"
        n_county_owner += 1
    if acres and acres > 0:
        cands.at[cands.index[i], "parcel_acres"] = acres
        cands.at[cands.index[i], "cad_acres"] = str(round(acres, 1))
        n_county_acres += 1
    if imp_val is not None:
        cands.at[cands.index[i], "cad_imp_value"] = str(int(imp_val))
    n_county += 1
    time.sleep(0.3)

    if n_county % 20 == 0 or n_county == sum(
        1 for _, r in cands.iterrows()
        if str(r.get("County","")).upper().strip().replace(" COUNTY","") in COUNTY_PARCEL_SERVICES
    ):
        print(f"    {n_county} county queries, {n_county_owner} owners, {n_county_acres} acreages …")

counties_covered = set(COUNTY_PARCEL_SERVICES.keys())
n_in_covered = sum(1 for _, r in cands.iterrows()
                   if str(r.get("County","")).upper().strip().replace(" COUNTY","") in counties_covered)
print(f"  Counties with parcel service: {', '.join(sorted(counties_covered))}")
print(f"  Candidates in covered counties: {n_in_covered}/{len(cands)}")
print(f"  Owner from county GIS: {n_county_owner} | Acreage from county GIS: {n_county_acres}")


# ===================================================================
# 4. OSM polygon acreage fallback (for uncovered counties)
# ===================================================================
print("\n" + "=" * 60)
print("4. OSM Industrial Polygon Acreage (fallback)")
print("=" * 60)

def compute_osm_acres(lat, lon, radius_m=500):
    query = f"""
    [out:json][timeout:30];
    (
      way["landuse"="industrial"](around:{radius_m},{lat},{lon});
      way["man_made"="works"](around:{radius_m},{lat},{lon});
      relation["landuse"="industrial"](around:{radius_m},{lat},{lon});
    );
    out geom;
    """
    try:
        r = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            headers=HEADERS, timeout=30,
        )
        if r.status_code != 200:
            return None
        elements = r.json().get("elements", [])
        if not elements:
            return None

        best_acres = 0
        for el in elements:
            geom_pts = el.get("geometry", [])
            if len(geom_pts) < 3:
                continue
            coords = [(g["lon"], g["lat"]) for g in geom_pts]
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            try:
                poly = Polygon(coords)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                lat_m = 111320
                lon_m = 111320 * math.cos(math.radians(lat))
                area_m2 = abs(poly.area) * lat_m * lon_m
                area_acres = area_m2 / 4046.86
                if area_acres > best_acres:
                    best_acres = area_acres
            except Exception:
                continue
        return best_acres if best_acres > 0 else None
    except Exception:
        return None

# Only query OSM for sites that still lack acreage
needs_acres = cands["parcel_acres"].isna()
n_need = needs_acres.sum()
print(f"  {n_need} sites still need acreage (not in covered counties or county query failed)")
print(f"  Querying Overpass (1 req/1.5s rate limit) …")

n_queried = 0
n_found = 0
for i in range(len(cands)):
    if not needs_acres.iloc[i]:
        continue
    pt = cands_wgs.geometry.iloc[i]
    acres = compute_osm_acres(pt.y, pt.x)
    if acres is not None:
        cands.at[cands.index[i], "parcel_acres"] = acres
        cands.at[cands.index[i], "osm_acres"] = str(round(acres, 1))
        n_found += 1
    n_queried += 1
    if n_queried % 25 == 0 or n_queried == n_need:
        print(f"    {n_queried}/{n_need} queried, {n_found} polygons found …")
    time.sleep(1.5)

print(f"  OSM fallback: {n_found}/{n_need} additional sites with acreage")


# ===================================================================
# Summary and save
# ===================================================================
print("\n" + "=" * 60)
print("Summary")
print("=" * 60)

n_owner = (cands["current_owner"].astype(str).str.strip() != "").sum()
n_acres = cands["parcel_acres"].notna().sum()
print(f"  Owner populated: {n_owner}/{len(cands)}")
print(f"  Acreage populated: {n_acres}/{len(cands)}")

# Show top candidates with owner and acreage
print("\n  Sample (top scored):")
cands_sorted = cands.sort_values("total_score" if "total_score" in cands.columns else "Plant_Name",
                                  ascending=False)
for _, r in cands_sorted.head(20).iterrows():
    owner = str(r.get("current_owner", ""))[:35]
    acres = f"{r['parcel_acres']:.0f} ac" if pd.notna(r.get("parcel_acres")) else "n/a"
    print(f"    {str(r['Plant_Name'])[:40]:<42s} {owner:<37s} {acres}")

# Save
out_gpkg = PROC_DIR / f"candidates_enriched_{STATE}.gpkg"
out_csv  = PROC_DIR / f"candidates_enriched_{STATE}.csv"
cands.to_file(out_gpkg, driver="GPKG")
cands.drop(columns="geometry").to_csv(out_csv, index=False)
print(f"\nSaved: {out_gpkg}")
print(f"Run score_and_export.py next.")
