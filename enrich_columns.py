"""
enrich_columns.py
Fills empty and partial columns in candidates_enriched_al.gpkg using
every available free programmatic data source.

Targets:
  water_basin          USGS WBD HUC8 web service (per-site query)
  pipeline_best_operator  Derived from pipeline_operators_5mi
  pipeline_max_dia_inches Extracted from pipeline_dia_est
  parcel_acres / owner    County GIS: Jefferson, Mobile, Tuscaloosa,
                          Calhoun, Montgomery, Shelby (newly found)
  soil_drainage        SSURGO re-query for gaps
  county_generation_mw Fix county name normalisation
  transmission_utility Fix county name normalisation
  nri_* / naics_code   Fix lookup gaps

Run after enrich_retirement.py and before fetch_parcels.py / score_and_export.py.
"""

import re
import time
import warnings
import requests
import numpy as np
import pandas as pd
import geopandas as gpd
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT     = Path(__file__).parent
PROC_DIR = ROOT / "data" / "processed"
AL_RAW   = ROOT / "data" / "westvirginia" / "raw"
STATE    = "tn"
HEADERS  = {"User-Agent": "DataCenterScreener/1.0"}

print("=" * 60)
print("Column Enrichment — Filling all available sources")
print("=" * 60)

src_path = PROC_DIR / f"candidates_enriched_{STATE}.gpkg"
cands = gpd.read_file(src_path)
cands_wgs = cands.to_crs("EPSG:4326")
print(f"\nLoaded {len(cands)} candidates\n")

# Initialise any columns that don't yet exist in the GPKG
for col in ["water_basin", "water_permitted_af", "pipeline_best_operator",
            "pipeline_max_dia_inches", "cad_imp_value",
            "soil_type", "soil_drainage", "soil_hydric",
            "county_generation_mw", "transmission_utility", "naics_code"]:
    if col not in cands.columns:
        cands[col] = np.nan if col in ("pipeline_max_dia_inches", "county_generation_mw") else ""


# ===================================================================
# 1. water_basin — USGS WBD HUC8 per-site query
# ===================================================================
print("=" * 60)
print("1. water_basin — USGS Watershed Boundary Dataset")
print("=" * 60)

WBD_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/4/query"
needs_basin = cands["water_basin"].astype(str).str.strip().isin(["", "nan"]) | cands["water_basin"].isna()
print(f"  {needs_basin.sum()} sites need water basin")

n_wbd = 0
for i in range(len(cands)):
    if not needs_basin.iloc[i]:
        continue
    pt = cands_wgs.geometry.iloc[i]
    try:
        r = requests.get(WBD_URL, params={
            "geometry": f"{pt.x},{pt.y}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "name,huc8",
            "returnGeometry": "false",
            "f": "json",
        }, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            feats = r.json().get("features", [])
            if feats:
                name = feats[0]["attributes"].get("name", "")
                huc  = feats[0]["attributes"].get("huc8", "")
                cands.at[cands.index[i], "water_basin"] = f"{name} (HUC8: {huc})"
                n_wbd += 1
    except Exception:
        pass
    time.sleep(0.2)

print(f"  Filled: {n_wbd}/{needs_basin.sum()}")


# ===================================================================
# 2. pipeline_best_operator — from pipeline_operators_5mi / pipeline_operator
# ===================================================================
print("\n" + "=" * 60)
print("2. pipeline_best_operator")
print("=" * 60)

needs_best = cands["pipeline_best_operator"].astype(str).str.strip().isin(["", "nan"]) | cands["pipeline_best_operator"].isna()
n_best = 0
for i in range(len(cands)):
    if not needs_best.iloc[i]:
        continue
    ops = str(cands.iloc[i].get("pipeline_operators_5mi", ""))
    if ops and ops != "nan":
        cands.at[cands.index[i], "pipeline_best_operator"] = ops.split(";")[0].strip()
        n_best += 1
    else:
        op = str(cands.iloc[i].get("pipeline_operator", ""))
        if op and op != "nan":
            cands.at[cands.index[i], "pipeline_best_operator"] = op.strip()
            n_best += 1

print(f"  Filled: {n_best}/{needs_best.sum()}")


# ===================================================================
# 3. pipeline_max_dia_inches — extracted from pipeline_dia_est
# ===================================================================
print("\n" + "=" * 60)
print("3. pipeline_max_dia_inches — from pipeline_dia_est")
print("=" * 60)

needs_dia = cands["pipeline_max_dia_inches"].isna()
n_dia = 0
for i in range(len(cands)):
    if not needs_dia.iloc[i]:
        continue
    dia_str = str(cands.iloc[i].get("pipeline_dia_est", ""))
    if dia_str and dia_str != "nan":
        nums = re.findall(r"(\d+)", dia_str)
        if nums:
            cands.at[cands.index[i], "pipeline_max_dia_inches"] = int(nums[-1])
            n_dia += 1

print(f"  Filled: {n_dia}/{needs_dia.sum()}")


# ===================================================================
# 4. County GIS parcel services — owner + acreage
#    Jefferson, Mobile, Tuscaloosa (existing) + Calhoun, Montgomery (new)
# ===================================================================
print("\n" + "=" * 60)
print("4. County GIS parcel services")
print("=" * 60)

# Unlike Tennessee (whose statewide layer only covers ~90 of 95 counties,
# with Davidson/Shelby/Knox/Hamilton/Rutherford/Montgomery needing their own
# county-specific fallback endpoints), West Virginia's WVGIS statewide parcel
# layer is a single composite covering all 55 counties — see fetch_parcels_wv.py
# for how it was verified. No county-by-county fallback list is needed here.
STATEWIDE_SVC = {
    "url": "https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels/MapServer/0/query",
    "owner": "FullOwnerName", "acres": "Acres_C",
}

COUNTY_SERVICES = {}

def query_parcel_svc(svc, lon, lat):
    out_fields = ",".join(f for f in [
        svc.get("owner"), svc.get("acres"), svc.get("value"), svc.get("imp"),
    ] if f)
    # WVGIS's WV_Parcels service errors out entirely (400 "Failed to execute
    # query") on point+distance queries AND on an unrecognized "Shape__Area"
    # outField — both verified live. It only accepts an envelope with just
    # the fields it actually has. ~0.002° (~180m at WV's latitude) matches
    # the old 200m point-buffer radius; Acres_C is always populated so the
    # Shape__Area fallback this function used for TN isn't needed here.
    pad = 0.002
    try:
        r = requests.get(svc["url"], params={
            "geometry": f"{lon-pad},{lat-pad},{lon+pad},{lat+pad}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
        }, headers=HEADERS, timeout=15, verify=False)
        feats = r.json().get("features", [])
        if not feats:
            return None, None, None
        acres_field = svc.get("acres", "")
        owner_field = svc.get("owner", "")
        # Pick largest parcel
        best = max(feats, key=lambda f: float(f["attributes"].get(acres_field, 0) or 0))
        a = best["attributes"]
        acres = float(a.get(acres_field, 0) or 0) or None
        owner = str(a.get(owner_field, "")).strip() if owner_field else None
        imp   = float(a.get(svc.get("imp", ""), 0) or 0) or None
        return owner, acres, imp
    except Exception:
        return None, None, None

n_cty = 0
for i in range(len(cands)):
    county = str(cands.iloc[i].get("County", "")).upper().strip().replace(" COUNTY", "")
    pt = cands_wgs.geometry.iloc[i]

    owner, acres, imp = query_parcel_svc(STATEWIDE_SVC, pt.x, pt.y)
    source = "WV_STATEWIDE"
    if (not owner and not acres) and county in COUNTY_SERVICES:
        owner, acres, imp = query_parcel_svc(COUNTY_SERVICES[county], pt.x, pt.y)
        source = f"COUNTY_GIS_{county}"

    if owner:
        cands.at[cands.index[i], "current_owner"] = owner
        cands.at[cands.index[i], "owner_source"] = source
    if acres and acres > 0:
        cands.at[cands.index[i], "parcel_acres"] = acres
        cands.at[cands.index[i], "cad_acres"] = str(round(acres, 1))
    if imp:
        cands.at[cands.index[i], "cad_imp_value"] = str(int(imp))
    if owner or acres:
        n_cty += 1
    time.sleep(0.2)

print(f"  Parcel matches (statewide + county GIS): {n_cty}")


# ===================================================================
# 5. soil_drainage gaps — SSURGO re-query
# ===================================================================
print("\n" + "=" * 60)
print("5. soil_drainage — SSURGO gap fill")
print("=" * 60)

SSURGO_URL = "https://SDMDataAccess.sc.egov.usda.gov/Tabular/post.rest"
needs_soil = (cands["soil_drainage"].astype(str).str.strip().isin(["", "nan"]) | cands["soil_drainage"].isna())
print(f"  {needs_soil.sum()} sites need soil drainage")

n_soil = 0
for i in range(len(cands)):
    if not needs_soil.iloc[i]:
        continue
    pt = cands_wgs.geometry.iloc[i]
    query = f"""
    SELECT TOP 1 c.compname, c.drainagecl, c.hydricrating
    FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('POINT({pt.x} {pt.y})') mk
    INNER JOIN mapunit mu ON mk.mukey = mu.mukey
    INNER JOIN component c ON mu.mukey = c.mukey
    WHERE c.comppct_r > 10
    ORDER BY c.comppct_r DESC
    """
    try:
        r = requests.post(SSURGO_URL, json={"query": query, "format": "JSON"}, timeout=20)
        if r.status_code == 200:
            rows = r.json().get("Table", [])
            if rows:
                cands.at[cands.index[i], "soil_type"]     = str(rows[0][0] or "")
                cands.at[cands.index[i], "soil_drainage"]  = str(rows[0][1] or "")
                cands.at[cands.index[i], "soil_hydric"]    = str(rows[0][2] or "")
                n_soil += 1
    except Exception:
        pass
    time.sleep(0.3)

print(f"  Filled: {n_soil}/{needs_soil.sum()}")


# ===================================================================
# 6. county_generation_mw / transmission_utility — fix name normalisation
# ===================================================================
print("\n" + "=" * 60)
print("6. county_generation_mw / transmission_utility — name fix")
print("=" * 60)

gen_operable = pd.read_excel(
    ROOT / "data" / "raw" / "eia860" / "3_1_Generator_Y2023.xlsx",
    sheet_name="Operable", header=1,
)
al_gen = gen_operable[gen_operable["State"] == "WV"].copy()
al_gen["Nameplate Capacity (MW)"] = pd.to_numeric(al_gen["Nameplate Capacity (MW)"], errors="coerce")
# Normalise county names to uppercase
al_gen["_county"] = al_gen["County"].str.upper().str.strip()
county_gen_mw = al_gen.groupby("_county")["Nameplate Capacity (MW)"].sum().to_dict()

plant_al = pd.read_excel(
    ROOT / "data" / "raw" / "eia860" / "2___Plant_Y2023.xlsx",
    sheet_name="Plant", header=1,
)
plant_al = plant_al[plant_al["State"] == "WV"].copy()
plant_al["_county"] = plant_al["County"].str.upper().str.strip()
county_utility = {}
for _, p in plant_al.iterrows():
    c = p["_county"]
    u = str(p.get("Transmission or Distribution System Owner", ""))
    if c and u and u != "nan":
        county_utility[c] = u

n_gen = n_util = 0
for i in range(len(cands)):
    county = str(cands.iloc[i].get("County", "")).upper().strip().replace(" COUNTY", "")
    mw = county_gen_mw.get(county, 0)
    if mw > 0 and (pd.isna(cands.iloc[i].get("county_generation_mw")) or cands.iloc[i].get("county_generation_mw") == 0):
        cands.at[cands.index[i], "county_generation_mw"] = int(mw)
        n_gen += 1
    u = county_utility.get(county, "")
    if u and (not cands.iloc[i].get("transmission_utility") or str(cands.iloc[i].get("transmission_utility")) == "nan"):
        cands.at[cands.index[i], "transmission_utility"] = u
        n_util += 1

print(f"  county_generation_mw: {n_gen} newly filled")
print(f"  transmission_utility: {n_util} newly filled")


# ===================================================================
# 7. naics_code gaps — fill from FRS data
# ===================================================================
print("\n" + "=" * 60)
print("7. naics_code — fill gaps from FRS")
print("=" * 60)

frs_path = ROOT / "data" / "raw" / "frs" / "AL_NAICS_FILE.CSV"
naics_path = ROOT / "data" / "raw" / "frs" / "AL_NAICS_FILE.CSV"
if naics_path.exists():
    naics_df = pd.read_csv(naics_path, low_memory=False, encoding="latin-1")
    naics_df.columns = [c.strip().upper() for c in naics_df.columns]
    id_col = "REGISTRY_ID" if "REGISTRY_ID" in naics_df.columns else naics_df.columns[0]
    code_col = next((c for c in naics_df.columns if "NAICS" in c and "CODE" in c), None)
    if code_col:
        naics_lookup = naics_df.drop_duplicates(id_col).set_index(id_col)[code_col].to_dict()
        n_naics = 0
        for i in range(len(cands)):
            if pd.notna(cands.iloc[i].get("naics_code")) and str(cands.iloc[i].get("naics_code")) not in ("", "nan"):
                continue
            sid = str(cands.iloc[i].get("site_id", "")).replace("FRS_", "")
            code = naics_lookup.get(sid) or naics_lookup.get(int(sid) if sid.isdigit() else sid)
            if code:
                cands.at[cands.index[i], "naics_code"] = str(code)[:3]
                n_naics += 1
        print(f"  Filled: {n_naics} NAICS codes")
    else:
        print("  [skip] No NAICS code column found")
else:
    print("  [skip] NAICS file not found")


# ===================================================================
# Summary
# ===================================================================
print("\n" + "=" * 60)
print("Fill rate after enrichment")
print("=" * 60)

targets = [
    "water_basin", "pipeline_best_operator", "pipeline_max_dia_inches",
    "parcel_acres", "cad_acres", "soil_drainage",
    "county_generation_mw", "transmission_utility", "naics_code",
]
for col in targets:
    if col in cands.columns:
        filled = ((cands[col].astype(str).str.strip() != "") & (cands[col].astype(str) != "nan") & cands[col].notna()).sum()
        print(f"  {col:<35s} {filled:>4}/{len(cands)} ({filled/len(cands)*100:.0f}%)")

# Save
cands.to_file(PROC_DIR / f"candidates_enriched_{STATE}.gpkg", driver="GPKG")
cands.drop(columns="geometry").to_csv(PROC_DIR / f"candidates_enriched_{STATE}.csv", index=False)
print(f"\nSaved {src_path}")
print("Run score_and_export.py next.")
