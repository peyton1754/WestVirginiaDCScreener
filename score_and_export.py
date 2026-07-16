"""
score_and_export_al.py
Scores each filtered West Virginia candidate 0–120 across seven dimensions and exports
the top 1,000 (or all survivors if fewer) as CSV + GeoJSON.

Scoring rubric (120 pts total):
  Gas pipeline        20 pts  — distance to interstate/intrastate pipeline (BTM generation)
  Brownfield type     20 pts  — site type indicates scale and infrastructure
  Substation voltage  15 pts  — nearest substation kV (grid interconnect capacity)
  State tier          20 pts  — DC market maturity & policy environment
  Tx redundancy       10 pts  — count of 230kV+ lines within 5 miles
  Water access        10 pts  — named water source on record
  Labor/metro         10 pts  — distance to nearest large urban area (EPSG:5070)
  Utility rate        10 pts  — industrial ¢/kWh from EIA 861 (lower = better)
  Parcel acreage       5 pts  — confirmed acreage (neutral if unknown)

Output:
  outputs/csv/top_candidates_wv.csv
  outputs/geojson/top_candidates_wv.geojson
"""

import numpy as np
import pandas as pd
import geopandas as gpd
import requests
from pathlib import Path
from shapely.strtree import STRtree

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT     = Path(__file__).parent
PROC_DIR = ROOT / "data" / "processed"
STATE    = "wv"
OUT_CSV  = ROOT / "outputs" / "csv"
OUT_GJ   = ROOT / "outputs" / "geojson"
OUT_CSV.mkdir(parents=True, exist_ok=True)
OUT_GJ.mkdir(parents=True, exist_ok=True)

CRS          = "EPSG:5070"
TOP_N          = 1000
MIN_SCORE      = 28
URBAN_MIN_ALAND_M2 = 200_000_000

AL_RAW = ROOT / "data" / "westvirginia" / "raw"

# ---------------------------------------------------------------------------
# Scoring tables (same as TX except STATE_SCORE)
# ---------------------------------------------------------------------------

def score_gas_pipeline(dist_mi) -> float:
    try:
        d = float(dist_mi)
    except (TypeError, ValueError):
        return 5.0
    if not np.isfinite(d):
        return 5.0
    if d <= 0.5:  return 20.0
    if d <= 1.0:  return 17.0
    if d <= 2.0:  return 13.0
    if d <= 5.0:  return float(np.interp(d, [2, 5], [13, 7]))
    if d <= 10.0: return float(np.interp(d, [5, 10], [7, 0]))
    return 0.0


def score_substation_kv(kv) -> float:
    try:
        v = float(kv)
    except (TypeError, ValueError):
        return 0.0
    if v >= 500: return 15.0
    if v >= 345: return 12.0
    if v >= 230: return  9.0
    if v >= 138: return  5.0
    if v >= 115: return  2.0
    return 0.0


def score_tx_redundancy(n_lines: int) -> float:
    if n_lines >= 4: return 10.0
    if n_lines == 3: return  7.0
    if n_lines == 2: return  4.0
    if n_lines == 1: return  1.0
    return 0.0


def score_air_permit(has_permit) -> float:
    if has_permit is True or str(has_permit).lower() in ("true", "1", "yes"):
        return 5.0
    return 0.0


BROWNFIELD_SCORE = {
    "Gas":                   20,
    "Nuclear":               18,
    "Coal":                  14,
    "Biomass":               10,
    "Oil":                    8,
    "Petroleum Refinery":    18,
    "Steel/Metals Plant":    16,
    "Paper/Pulp Mill":       15,
    "Chemical Plant":        14,
    "Minerals/Cement Plant": 12,
    "Wood/Lumber Mill":      10,
    "Plastics/Rubber Plant": 10,
    "Fabricated Metals Plant": 9,
    "EPA ACRES Brownfield":  14,
    "Industrial":             8,
    "Industrial Machinery Plant": 7,
    "Electronics Mfg Plant":  6,
    "Other":                  5,
}

# Tennessee is an emerging market — low power costs (Tennessee Power/TVA),
# strong state incentives, growing interest from hyperscalers
STATE_SCORE = {
    "VA": 20,
    "TX": 18,
    "GA": 16,
    "NC": 14,
    "TN": 14,   # TVA power (~4-5 cents/kWh industrial), growing Nashville market, EPB Chattanooga fiber
    "WV": 11,   # AEP/Mon Power ~6.3 cents/kWh industrial, 2025 law enabling BTM power for "high impact data centers", smaller current DC market
    "OH": 10,
    "AR":  9,
}


def score_parcel(acres) -> float:
    try:
        a = float(acres)
    except (TypeError, ValueError):
        return 2.5
    if not np.isfinite(a) or a <= 0:
        return 2.5
    if a >= 500:  return 5.0
    if a >= 250:  return 4.0
    if a >= 100:  return 3.0
    if a >= 50:   return 2.0
    return 0.0


def score_water(dist_mi, permitted_af=None) -> float:
    """Score water access: distance to water body (8 pts) + permitted withdrawals (2 pts)."""
    if pd.isna(dist_mi):
        base = 5.0
    else:
        d = float(dist_mi)
        if d <= 0.5:  base = 8.0
        elif d <= 1.0:  base = float(np.interp(d, [0.5, 1.0], [8.0, 6.0]))
        elif d <= 2.0:  base = float(np.interp(d, [1.0, 2.0], [6.0, 4.0]))
        elif d <= 5.0:  base = float(np.interp(d, [2.0, 5.0], [4.0, 1.0]))
        else: base = 0.0
    # Bonus for documented permitted water withdrawals (indicates real water access)
    try:
        af = float(permitted_af)
        if np.isfinite(af) and af > 0:
            bonus = 2.0 if af >= 1000 else 1.0
        else:
            bonus = 0.0
    except (TypeError, ValueError):
        bonus = 0.0
    return float(np.clip(base + bonus, 0, 10))


def score_opportunity_zone(in_oz) -> float:
    """5 pts if site is in a federal Opportunity Zone tract."""
    return 5.0 if in_oz else 0.0


def score_flood_risk(flood_zone) -> float:
    """Penalize sites in FEMA 100-yr floodplain. 0 = high risk, 5 = moderate, 10 = minimal."""
    if not flood_zone or pd.isna(flood_zone):
        return 5.0  # unknown = neutral
    z = str(flood_zone).upper().strip()
    if z.startswith("A") or z.startswith("V"):
        return 0.0   # 100-yr (Special Flood Hazard Area)
    if z.startswith("X ") or z == "X":
        return 10.0  # minimal flood hazard
    if z.startswith("B") or z.startswith("C"):
        return 7.0   # moderate
    return 5.0


def score_metro(dist_m: float) -> float:
    max_dist = 60 * 1609.344
    score = 10 * (1 - dist_m / max_dist)
    return float(np.clip(score, 0, 10))


def score_seismic(ss) -> float:
    """Score seismic risk from USGS ASCE 7-22 Ss (short-period spectral accel, in g).
    Lower Ss = less shaking = better for data center construction.
    Memphis/West TN New Madrid zone: Ss ~1.5-2.0g → 0-2 pts.
    East TN ridge/valley: Ss ~0.1-0.4g → 8-10 pts."""
    try:
        v = float(ss)
    except (TypeError, ValueError):
        return 5.0  # unknown → neutral
    if not np.isfinite(v) or v < 0:
        return 5.0
    if v < 0.25:  return 10.0
    if v < 0.50:  return float(np.interp(v, [0.25, 0.50], [10.0, 8.0]))
    if v < 1.00:  return float(np.interp(v, [0.50, 1.00], [8.0,  5.0]))
    if v < 1.50:  return float(np.interp(v, [1.00, 1.50], [5.0,  2.0]))
    return float(np.interp(v, [1.50, 2.00], [2.0, 0.0]))


def score_fiber(mbps) -> float:
    """Score based on max advertised fiber download speed (FCC Form 477, techcode 50).
    0 = no fiber presence in census block; max 10 pts."""
    try:
        s = float(mbps)
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(s) or s <= 0:
        return 0.0
    if s >= 1000: return 10.0
    if s >= 100:  return 7.0
    if s >= 25:   return 4.0
    return 1.0


def build_county_rate_map() -> dict[str, float]:
    sales_path = AL_RAW / "utility_territories" / "eia861_2022" / "Sales_Ult_Cust_2022.xlsx"
    terr_path  = AL_RAW / "utility_territories" / "eia861_2022" / "Service_Territory_2022.xlsx"
    if not sales_path.exists() or not terr_path.exists():
        print("  [WARN] EIA 861 files not found — utility rate scoring disabled")
        return {}
    try:
        sales = pd.read_excel(sales_path, header=2)
        sales.columns = [str(c).strip() for c in sales.columns]
        col_state   = sales.columns[6]
        col_name    = sales.columns[2]
        col_ind_rev = sales.columns[15]
        col_ind_mwh = sales.columns[16]
        al_sales = sales[sales[col_state].astype(str).str.upper().str.strip() == "WV"].copy()
        al_sales[col_ind_rev] = pd.to_numeric(al_sales[col_ind_rev], errors="coerce")
        al_sales[col_ind_mwh] = pd.to_numeric(al_sales[col_ind_mwh], errors="coerce")
        grp = al_sales.groupby(col_name)[[col_ind_rev, col_ind_mwh]].sum()
        grp = grp[grp[col_ind_mwh] > 0]
        grp["cents_kwh"] = (grp[col_ind_rev] * 1000) / grp[col_ind_mwh] / 10
        util_rate = grp["cents_kwh"].to_dict()

        terr = pd.read_excel(terr_path, header=0)
        terr.columns = ["year", "util_num", "util_name", "short_form", "state", "county"]
        al_terr = terr[terr["state"].str.upper() == "WV"].copy()
        al_terr["county"] = al_terr["county"].str.upper().str.strip()
        al_terr["rate"] = al_terr["util_name"].map(util_rate)

        county_rate = (
            al_terr.dropna(subset=["rate"])
            .groupby("county")["rate"]
            .min()
            .to_dict()
        )
        print(f"  EIA 861 county→utility rates: {len(county_rate)} AL counties mapped")
        return county_rate
    except Exception as e:
        print(f"  [WARN] EIA 861 rate parsing failed: {e}")
        return {}


_COUNTY_RATES: dict[str, float] | None = None

def score_utility_rate(county) -> float:
    global _COUNTY_RATES
    if _COUNTY_RATES is None:
        _COUNTY_RATES = build_county_rate_map()

    if not _COUNTY_RATES:
        return 5.0

    key = str(county).upper().strip() if pd.notna(county) else ""
    key = key.replace(" COUNTY", "").replace(" PARISH", "").strip()
    rate = _COUNTY_RATES.get(key)
    if rate is None:
        return 5.0

    score = 10 * (1 - (rate - 5.0) / 5.0)
    return float(np.clip(score, 0, 10))


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
print("=" * 60)
print("Score & Export — West Virginia Data Center Candidate Ranking")
print("=" * 60)

enriched_path = PROC_DIR / f"candidates_enriched_{STATE}.gpkg"
filtered_path = PROC_DIR / f"candidates_filtered_{STATE}.gpkg"
if enriched_path.exists():
    gdf = gpd.read_file(enriched_path).to_crs(CRS)
    print(f"\nLoaded {len(gdf)} enriched candidates (post activity-verification)")
else:
    gdf = gpd.read_file(filtered_path).to_crs(CRS)
    print(f"\nLoaded {len(gdf)} filtered candidates (run enrich_candidates_al.py for activity verification)")

# ---------------------------------------------------------------------------
# Compute distances to nearest large urban area
# ---------------------------------------------------------------------------
print("\nComputing distance to nearest large urban area …")

census = gpd.read_file(
    ROOT / "data" / "raw" / "census" / "tl_2023_us_uac20.shp"
).to_crs(CRS)
census_large = census[census["ALAND20"] >= URBAN_MIN_ALAND_M2].copy()

tree = STRtree(census_large.geometry.values)
nearest_idx = tree.nearest(gdf.geometry.values)
nearest_geoms = census_large.geometry.values[nearest_idx]
dist_to_metro = np.array([
    pt.distance(ng) for pt, ng in zip(gdf.geometry.values, nearest_geoms)
])
nearest_metro_name = census_large["NAME20"].values[nearest_idx]

gdf["dist_to_metro_mi"] = dist_to_metro / 1609.344
gdf["nearest_metro"]    = nearest_metro_name

# ---------------------------------------------------------------------------
# Hard-exclude sites marked EXCLUDED by enrich_retirement.py
# ---------------------------------------------------------------------------
if "retirement_confidence" in gdf.columns:
    n_before = len(gdf)
    excluded = gdf[gdf["retirement_confidence"] == "EXCLUDED"]
    if len(excluded):
        print(f"\nHard-excluding {len(excluded)} sites with EXCLUDED confidence "
              f"(AIR_PERMIT_OPERATING or RECENTLY_INSPECTED):")
        for _, row in excluded.iterrows():
            print(f"  ✗ {row['Plant_Name']}")
        gdf = gdf[gdf["retirement_confidence"] != "EXCLUDED"].reset_index(drop=True)
        print(f"  {n_before} → {len(gdf)} candidates")

    n_before = len(gdf)
    active_warn = gdf[gdf["retirement_confidence"] == "ACTIVE_WARNING"]
    if len(active_warn):
        print(f"\nHard-excluding {len(active_warn)} sites with ACTIVE_WARNING confidence:")
        for _, row in active_warn.iterrows():
            print(f"  ✗ {row['Plant_Name']}")
        gdf = gdf[gdf["retirement_confidence"] != "ACTIVE_WARNING"].reset_index(drop=True)
        print(f"  {n_before} → {len(gdf)} candidates")

# ---------------------------------------------------------------------------
# Hard-exclude sites with confirmed parcel < 25 acres
# (sites with unknown acreage pass through — scored conservatively)
# ---------------------------------------------------------------------------
MIN_CONFIRMED_ACRES = 25
n_before = len(gdf)

def best_acres(row):
    for col in ("parcel_acres", "cad_acres", "osm_acres"):
        v = row.get(col)
        try:
            f = float(v)
            if f > 0:
                return f
        except (TypeError, ValueError):
            pass
    return None

confirmed_small = []
keep_mask = []
for _, row in gdf.iterrows():
    ac = best_acres(row)
    if ac is not None and ac < MIN_CONFIRMED_ACRES:
        confirmed_small.append(row["Plant_Name"])
        keep_mask.append(False)
    else:
        keep_mask.append(True)

keep_mask = pd.Series(keep_mask, index=gdf.index)
if confirmed_small:
    print(f"\nHard-excluding {len(confirmed_small)} sites with confirmed parcel < {MIN_CONFIRMED_ACRES} acres:")
    for name in confirmed_small:
        print(f"  ✗ {name}")
    gdf = gdf[keep_mask].reset_index(drop=True)
    print(f"  {n_before} → {len(gdf)} candidates")

# ---------------------------------------------------------------------------
# Apply scoring
# ---------------------------------------------------------------------------
print("Scoring candidates …")
print("  Loading substation and transmission data …")

_sub_pq = ROOT / "data" / "raw" / "hifld" / "substations.parquet"
_sub_gj = ROOT / "data" / "raw" / "hifld" / "substations.geojson"
subs = (gpd.read_parquet(_sub_pq) if _sub_pq.exists()
        else gpd.read_file(_sub_gj)).to_crs(CRS)

def parse_max_kv(v):
    try:
        return max(float(p.strip()) for p in str(v).split(";") if p.strip()) / 1000
    except Exception:
        return 0.0

subs["max_kv"] = subs["voltage"].apply(parse_max_kv)
subs_hv = subs[subs["max_kv"] >= 115]
tree_sub = STRtree(subs_hv.geometry.values)
nearest_sub_idx = tree_sub.nearest(gdf.geometry.values)
gdf["nearest_sub_kv"] = subs_hv["max_kv"].values[nearest_sub_idx]
gdf["dist_to_sub_mi"] = np.array([
    pt.distance(subs_hv.geometry.values[i])
    for pt, i in zip(gdf.geometry.values, nearest_sub_idx)
]) / 1609.344
del subs, subs_hv

print("  Loading transmission lines …")
_tx_pq = ROOT / "data" / "raw" / "hifld" / "transmission_lines.parquet"
_tx_gj = ROOT / "data" / "raw" / "hifld" / "transmission_lines.geojson"
tx = (gpd.read_parquet(_tx_pq) if _tx_pq.exists()
      else gpd.read_file(_tx_gj)).to_crs(CRS)
tx["VOLTAGE"] = pd.to_numeric(tx["VOLTAGE"], errors="coerce")
tx_hv = tx[(tx["VOLTAGE"] >= 230) & (tx["STATUS"] == "IN SERVICE")]
from collections import Counter
results_tx = STRtree(tx_hv.geometry.values).query(
    gdf.geometry.values, predicate="dwithin", distance=5 * 1609.344
)
tx_counts = Counter(results_tx[0].tolist())
gdf["tx_lines_5mi"] = [tx_counts.get(i, 0) for i in range(len(gdf))]
del tx, tx_hv

# ---------------------------------------------------------------------------
# Pipeline capacity estimation (diameter from operator + type)
# ---------------------------------------------------------------------------
print("  Estimating pipeline capacity …")

PIPE_DIAMETER = {
    "Southern Natural Gas":    "20-30\"",
    "Gulf South Pipeline":     "20-30\"",
    "Transcontinental Gas":    "30-42\"",
    "Tennessee Gas Pipeline":  "24-36\"",
    "Enbridge Pipelines":      "20-36\"",
    "Southeast Supply Head":   "36\"",
    "Florida Gas Trans":       "24-36\"",
    "Mountain Valley Pipeline": "42\"",   # confirmed mainline diameter, WV/VA
    "Columbia Gas Transmission": "24-30\"",
    "Texas Eastern":           "30-42\"",
    "Equitrans":               "20-30\"",
}

gdf["pipeline_dia_est"] = ""
gdf["pipeline_pressure_psi"] = np.nan
if "pipeline_operator" in gdf.columns:
    for i in range(len(gdf)):
        op = str(gdf.iloc[i].get("pipeline_operator", ""))
        ptype = str(gdf.iloc[i].get("pipeline_type", ""))
        matched = False
        for known_op, dia in PIPE_DIAMETER.items():
            if known_op.upper()[:12] in op.upper():
                gdf.at[gdf.index[i], "pipeline_dia_est"] = dia
                gdf.at[gdf.index[i], "pipeline_pressure_psi"] = 1000
                matched = True
                break
        if not matched:
            if "Interstate" in ptype:
                gdf.at[gdf.index[i], "pipeline_dia_est"] = "20-36\" (Interstate)"
                gdf.at[gdf.index[i], "pipeline_pressure_psi"] = 1000
            elif "Intrastate" in ptype:
                gdf.at[gdf.index[i], "pipeline_dia_est"] = "6-20\" (Intrastate)"
                gdf.at[gdf.index[i], "pipeline_pressure_psi"] = 300

# Extract max diameter number from dia_est string for pipeline_max_dia_inches
import re as _re_dia
for i in range(len(gdf)):
    if pd.isna(gdf.iloc[i].get("pipeline_max_dia_inches")) or str(gdf.iloc[i].get("pipeline_max_dia_inches","")) in ("","nan"):
        dia_str = str(gdf.iloc[i].get("pipeline_dia_est", ""))
        nums = _re_dia.findall(r"(\d+)", dia_str)
        if nums:
            gdf.at[gdf.index[i], "pipeline_max_dia_inches"] = int(nums[-1])

# Fill pipeline_best_operator from operators_5mi if not already set
for i in range(len(gdf)):
    if str(gdf.iloc[i].get("pipeline_best_operator","")) in ("","nan"):
        ops = str(gdf.iloc[i].get("pipeline_operators_5mi",""))
        if ops and ops != "nan":
            gdf.at[gdf.index[i], "pipeline_best_operator"] = ops.split(";")[0].strip()
        elif str(gdf.iloc[i].get("pipeline_operator","")) not in ("","nan"):
            gdf.at[gdf.index[i], "pipeline_best_operator"] = str(gdf.iloc[i]["pipeline_operator"])

# ---------------------------------------------------------------------------
# Grid capacity estimation (substation MVA + line capacity + county generation)
# ---------------------------------------------------------------------------
print("  Estimating grid capacity …")

def est_sub_mva(kv):
    if kv >= 500: return "1000-2000"
    if kv >= 345: return "500-1000"
    if kv >= 230: return "200-500"
    if kv >= 138: return "50-200"
    if kv >= 115: return "30-100"
    return "<30"

def est_line_mw(kv):
    if kv >= 500: return 2000
    if kv >= 345: return 500
    if kv >= 230: return 250
    return 100

# County generation from EIA 860
gen_operable = pd.read_excel(
    ROOT / "data" / "raw" / "eia860" / "3_1_Generator_Y2023.xlsx",
    sheet_name="Operable", header=1,
)
al_gen = gen_operable[gen_operable["State"] == "WV"]
al_gen["Nameplate Capacity (MW)"] = pd.to_numeric(al_gen["Nameplate Capacity (MW)"], errors="coerce")
county_gen_mw = al_gen.groupby(al_gen["County"].str.upper().str.strip())["Nameplate Capacity (MW)"].sum().to_dict()

# Transmission utility from EIA 860 Plant
plant_al = pd.read_excel(
    ROOT / "data" / "raw" / "eia860" / "2___Plant_Y2023.xlsx",
    sheet_name="Plant", header=1,
)
plant_al = plant_al[plant_al["State"] == "WV"]
county_utility = {}
for _, p in plant_al.iterrows():
    c = str(p.get("County", "")).upper().strip()
    u = str(p.get("Transmission or Distribution System Owner", ""))
    if c and u and u != "nan":
        county_utility[c] = u

gdf["sub_capacity_mva_est"] = ""
gdf["grid_capacity_mw_est"] = np.nan
gdf["county_generation_mw"] = np.nan
gdf["transmission_utility"] = ""

for i in range(len(gdf)):
    county = str(gdf.iloc[i].get("County", "")).upper().strip().replace(" COUNTY", "")
    sub_kv = float(gdf.iloc[i].get("nearest_sub_kv", 0) or 0)
    n_lines = int(gdf.iloc[i].get("tx_lines_5mi", 0) or 0)

    mva_str = est_sub_mva(sub_kv)
    gdf.at[gdf.index[i], "sub_capacity_mva_est"] = mva_str

    if sub_kv >= 115 and n_lines >= 1:
        mva_mid = {"1000-2000": 1500, "500-1000": 750, "200-500": 350,
                   "50-200": 125, "30-100": 65, "<30": 15}[mva_str]
        gdf.at[gdf.index[i], "grid_capacity_mw_est"] = int(
            mva_mid + n_lines * est_line_mw(min(sub_kv, 230))
        )

    gen_mw = county_gen_mw.get(county, 0)
    if gen_mw > 0:
        gdf.at[gdf.index[i], "county_generation_mw"] = int(gen_mw)

    u = county_utility.get(county, "")
    if u:
        gdf.at[gdf.index[i], "transmission_utility"] = u

n_grid = gdf["grid_capacity_mw_est"].notna().sum()
print(f"  Grid capacity estimated: {n_grid}/{len(gdf)}")
print(f"  County generation mapped: {(gdf['county_generation_mw'].notna()).sum()}/{len(gdf)}")

print("  Loading utility rate table …")

# ---------------------------------------------------------------------------
# FCC Broadband — fiber presence per census block
# ---------------------------------------------------------------------------
print("\nEnriching fiber connectivity (FCC Form 477, techcode 50) …")
fcc_csv  = AL_RAW / "broadband" / "fcc_477_al_fiber.csv"
blk_zip  = AL_RAW / "broadband" / "tl_2020_54_tabblock20.zip"
blk_dir  = AL_RAW / "broadband" / "blocks"

# Download FCC fiber records for WV if not cached
if not fcc_csv.exists():
    fcc_csv.parent.mkdir(parents=True, exist_ok=True)
    print("  Downloading FCC Form 477 fiber data for WV …")
    try:
        rows, offset, limit = [], 0, 50000
        while True:
            r = requests.get(
                "https://opendata.fcc.gov/resource/jdr4-3q4p.json",
                params={"$limit": limit, "$offset": offset,
                        "$where": "stateabbr = 'WV' AND techcode = 50",
                        "$select": "blockcode,maxaddown,maxadup"},
                timeout=60,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            offset += len(batch)
            if len(batch) < limit:
                break
        if rows:
            pd.DataFrame(rows).to_csv(fcc_csv, index=False)
            print(f"    → {len(rows):,} fiber census-block records")
    except Exception as e:
        print(f"  [WARN] FCC download failed: {e}")

# Download WV census block shapefile if not cached
if not blk_zip.exists():
    blk_zip.parent.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request
        print("  Downloading WV census blocks (TIGER 2020) …")
        urllib.request.urlretrieve(
            "https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/tl_2020_54_tabblock20.zip",
            blk_zip,
        )
        print(f"    → {blk_zip.stat().st_size / 1e6:.1f} MB")
    except Exception as e:
        print(f"  [WARN] Census block download failed: {e}")

if blk_zip.exists() and not blk_dir.exists():
    import zipfile
    blk_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(blk_zip) as z:
        z.extractall(blk_dir)

# Join FCC fiber speeds to each site via census block spatial join
if fcc_csv.exists() and fcc_csv.stat().st_size > 100 and blk_dir.exists():
    try:
        fcc = pd.read_csv(fcc_csv, dtype={"blockcode": str})
        fcc["maxaddown"] = pd.to_numeric(fcc["maxaddown"], errors="coerce").fillna(0)
        # keep max speed per block
        fcc_max = fcc.groupby("blockcode")["maxaddown"].max().reset_index()
        fcc_max.columns = ["GEOID20", "fiber_max_speed_mbps"]

        blk_shp = next(blk_dir.glob("*.shp"))
        blocks = gpd.read_file(blk_shp)[["GEOID20", "geometry"]].to_crs(CRS)
        blocks = blocks.merge(fcc_max, on="GEOID20", how="left")
        blocks["fiber_max_speed_mbps"] = blocks["fiber_max_speed_mbps"].fillna(0)

        joined = gpd.sjoin(
            gdf[["geometry"]].copy(),
            blocks[["geometry", "fiber_max_speed_mbps"]],
            how="left", predicate="within",
        )
        # take max speed if a site falls in multiple blocks
        fiber_speeds = joined.groupby(joined.index)["fiber_max_speed_mbps"].max()
        gdf["fiber_max_speed_mbps"] = fiber_speeds.reindex(gdf.index).fillna(0)
        n_fiber = (gdf["fiber_max_speed_mbps"] > 0).sum()
        print(f"  Fiber presence: {n_fiber}/{len(gdf)} sites in fiber-served blocks "
              f"(avg {gdf['fiber_max_speed_mbps'].mean():.0f} Mbps)")
    except Exception as e:
        print(f"  [WARN] Fiber enrichment failed: {e}")
        if "fiber_max_speed_mbps" not in gdf.columns:
            gdf["fiber_max_speed_mbps"] = 0.0
else:
    if "fiber_max_speed_mbps" not in gdf.columns:
        gdf["fiber_max_speed_mbps"] = 0.0
    print("  [skip] FCC or census block data not available")

# ---------------------------------------------------------------------------
# Opportunity Zones
# ---------------------------------------------------------------------------
print("\nChecking Opportunity Zone eligibility …")
oz_csv   = AL_RAW / "opportunity_zones" / "oz_designations.csv"
tract_zip = AL_RAW / "opportunity_zones" / "tl_2023_47_tract.zip"
tract_dir = AL_RAW / "opportunity_zones" / "tracts"

if oz_csv.exists() and tract_zip.exists():
    try:
        if not tract_dir.exists():
            import zipfile as _zf
            tract_dir.mkdir(parents=True, exist_ok=True)
            with _zf.ZipFile(tract_zip) as z:
                z.extractall(tract_dir)

        oz = pd.read_csv(oz_csv, dtype={"geoid": str})
        oz_tracts = set(oz["geoid"].str.strip())

        tract_shp = next(tract_dir.glob("*.shp"))
        tracts = gpd.read_file(tract_shp)[["GEOID", "geometry"]].to_crs(CRS)
        tracts["in_oz"] = tracts["GEOID"].isin(oz_tracts)
        oz_tracts_gdf = tracts[tracts["in_oz"]]

        joined_oz = gpd.sjoin(
            gdf[["geometry"]].copy(),
            oz_tracts_gdf[["geometry"]],
            how="left", predicate="within",
        )
        gdf["opportunity_zone"] = gdf.index.isin(
            joined_oz.dropna(subset=["index_right"]).index
        )
        n_oz = gdf["opportunity_zone"].sum()
        print(f"  {n_oz}/{len(gdf)} sites in federal Opportunity Zones")
    except Exception as e:
        print(f"  [WARN] OZ enrichment failed: {e}")
        gdf["opportunity_zone"] = False
else:
    gdf["opportunity_zone"] = False
    print("  [skip] Opportunity Zone data not found")

# ---------------------------------------------------------------------------
# FEMA Flood Zone (NFIP)
# ---------------------------------------------------------------------------
print("\nChecking FEMA flood zone exposure …")
fema_zip = AL_RAW / "fema_nri" / "wv_nfip_flood_zones.zip"

if not fema_zip.exists():
    print("  Downloading FEMA NFIP flood zone data for WV …")
    try:
        import urllib.request as _ur
        # FEMA NFIP national flood hazard layer — West Virginia
        _ur.urlretrieve(
            "https://hazards.fema.gov/nfhl/rest/services/public/NFHL/MapServer/28/query"
            "?where=STATE_CD%3D%27WV%27&outFields=FLD_ZONE&f=geojson&resultRecordCount=50000",
            fema_zip,
        )
        print(f"    → {fema_zip.stat().st_size / 1e6:.1f} MB")
    except Exception as e:
        print(f"  [WARN] FEMA flood zone download failed: {e}")

# Use USGS WaterWatch or NRI flood risk as fallback
gdf["flood_zone"] = gdf.get("flood_zone", pd.Series("", index=gdf.index))
if fema_zip.exists() and fema_zip.stat().st_size > 1000:
    try:
        flood = gpd.read_file(fema_zip).to_crs(CRS)
        flood = flood[flood["FLD_ZONE"].notna()][["FLD_ZONE", "geometry"]]
        joined_fl = gpd.sjoin(
            gdf[["geometry"]].copy(),
            flood,
            how="left", predicate="within",
        )
        # pick the highest-risk zone if multiple (A > X)
        def _worst_zone(zones):
            for z in ["AE","AO","AH","A","VE","V"]:
                if z in zones.values:
                    return z
            if "X" in zones.values:
                return "X"
            return zones.iloc[0] if len(zones) else ""
        flood_zones = joined_fl.groupby(joined_fl.index)["FLD_ZONE"].apply(_worst_zone)
        gdf["flood_zone"] = flood_zones.reindex(gdf.index).fillna("X")
        n_sfha = gdf["flood_zone"].str.startswith("A").sum() + gdf["flood_zone"].str.startswith("V").sum()
        print(f"  {n_sfha}/{len(gdf)} sites in FEMA Special Flood Hazard Area (100-yr)")
    except Exception as e:
        print(f"  [WARN] Flood zone join failed: {e}")
        gdf["flood_zone"] = ""
else:
    print("  [skip] FEMA flood zone data not available — using NRI flood rating")

# ---------------------------------------------------------------------------
# EIA-860 retirement signal boost
# ---------------------------------------------------------------------------
print("\nApplying EIA-860 generator status cross-reference …")
eia860_zip = ROOT / "data" / "raw" / "eia860" / "eia8602023.zip"
if eia860_zip.exists():
    try:
        import zipfile as _zf860
        with _zf860.ZipFile(eia860_zip) as z:
            with z.open("3_1_Generator_Y2023.xlsx") as f:
                gen = pd.read_excel(f, header=1,
                                    usecols=["Plant Name", "State", "Status", "Nameplate Capacity (MW)"])
        gen = gen[gen["State"] == "WV"].copy()
        gen["Nameplate Capacity (MW)"] = pd.to_numeric(gen["Nameplate Capacity (MW)"], errors="coerce")
        # Retired/shutdown generators: OS=out of service, RE=retired, IP=indefinitely postponed
        retired = gen[gen["Status"].isin(["OS","RE","IP"])].copy()
        retired["_plant_upper"] = retired["Plant Name"].str.upper().str.strip()
        retired_plants = set(retired["_plant_upper"])

        def _eia860_confidence_boost(row):
            name = str(row.get("Plant_Name","")).upper().strip()
            # exact or partial match
            for rp in retired_plants:
                if rp in name or name in rp:
                    return True
            return False

        gdf["eia860_retired"] = gdf.apply(_eia860_confidence_boost, axis=1)
        n_matched = gdf["eia860_retired"].sum()
        print(f"  EIA-860 matched {n_matched} WV sites as retired/out-of-service generators")
    except Exception as e:
        print(f"  [WARN] EIA-860 cross-reference failed: {e}")
        gdf["eia860_retired"] = False
else:
    gdf["eia860_retired"] = False
    print("  [skip] EIA-860 data not found")

# --- Apply all scores ---
gdf["score_gas"]          = gdf["dist_to_pipeline_mi"].apply(score_gas_pipeline) \
                            if "dist_to_pipeline_mi" in gdf.columns \
                            else pd.Series(5.0, index=gdf.index)
gdf["score_substation"]   = gdf["nearest_sub_kv"].apply(score_substation_kv)
gdf["score_tx_redundancy"]= gdf["tx_lines_5mi"].apply(score_tx_redundancy)
gdf["score_brownfield"]   = gdf["brownfield_type"].map(BROWNFIELD_SCORE).fillna(5)
gdf["score_state"]        = gdf["State"].map(STATE_SCORE).fillna(0)
gdf["score_parcel"]       = gdf["parcel_acres"].apply(score_parcel)
gdf["score_water"]        = gdf.apply(
                                lambda r: score_water(r.get("dist_to_water_mi"),
                                                      r.get("water_permitted_af")), axis=1)
gdf["score_metro"]        = gdf["dist_to_metro_mi"].apply(
                                lambda d: score_metro(d * 1609.344))
gdf["score_utility_rate"] = gdf["County"].apply(score_utility_rate)
gdf["score_air_permit"]   = gdf["has_air_permit"].apply(score_air_permit) \
                            if "has_air_permit" in gdf.columns \
                            else pd.Series(0.0, index=gdf.index)
gdf["score_fiber"]        = gdf["fiber_max_speed_mbps"].apply(score_fiber)
gdf["score_opportunity_zone"] = gdf["opportunity_zone"].apply(score_opportunity_zone)
gdf["score_flood_risk"]   = gdf["flood_zone"].apply(score_flood_risk)

infra_score = (
    gdf["score_gas"]
    + gdf["score_substation"]
    + gdf["score_tx_redundancy"]
    + gdf["score_brownfield"]
    + gdf["score_state"]
    + gdf["score_parcel"]
    + gdf["score_water"]
    + gdf["score_metro"]
    + gdf["score_utility_rate"]
    + gdf["score_air_permit"]
    + gdf["score_fiber"]
    + gdf["score_opportunity_zone"]
    + gdf["score_flood_risk"]
)

RETIREMENT_MULTIPLIER = {
    "VERY_HIGH":      1.00,
    "HIGH":           1.00,
    "MEDIUM":         0.90,
    "LOW":            0.75,
    "ACTIVE_WARNING": 0.50,
    "UNVERIFIED":     0.70,
}

if "retirement_confidence" in gdf.columns:
    multiplier = gdf["retirement_confidence"].map(RETIREMENT_MULTIPLIER).fillna(0.70)
    # EIA-860 OS/RE match upgrades UNVERIFIED → 0.85 (between LOW and MEDIUM)
    if "eia860_retired" in gdf.columns:
        upgrade_mask = gdf["eia860_retired"] & (multiplier < 0.85)
        n_upgraded = upgrade_mask.sum()
        if n_upgraded:
            multiplier = multiplier.where(~upgrade_mask, 0.85)
            print(f"  EIA-860 retirement boost: {n_upgraded} sites upgraded to 0.85×")
    gdf["score_retirement_mult"] = multiplier
    gdf["infra_score"] = infra_score
    gdf["total_score"] = infra_score * multiplier

    for level, mult in RETIREMENT_MULTIPLIER.items():
        n = (gdf["retirement_confidence"] == level).sum()
        if n:
            print(f"  Retirement {level:18s}: {n:3d} sites × {mult:.0%}")
else:
    gdf["infra_score"] = infra_score
    gdf["total_score"] = infra_score
    gdf["score_retirement_mult"] = 1.0

# ---------------------------------------------------------------------------
# Ownership placeholders
# ---------------------------------------------------------------------------
for col in ["current_owner", "owner_source", "franchise_tax_status",
            "cad_acres", "osm_acres"]:
    if col not in gdf.columns:
        gdf[col] = ""

# ---------------------------------------------------------------------------
# DOE Energy Communities
# ---------------------------------------------------------------------------
print("\nChecking DOE Energy Community eligibility …")
ec_path = AL_RAW / "energy_communities" / "msa_nmsa_ffe.zip"
if ec_path.exists():
    try:
        ec_gdf = gpd.read_file(
            f"zip://{ec_path}!MSA_NMSA_EC_FFE_v2024_1/Shapefiles/MSA_NMSA_EC_v2024_1.shp"
        ).to_crs(CRS)
        ec_al = ec_gdf[ec_gdf["state_name"] == "West Virginia"]
        ec_counties = set(ec_al["county_nam"].str.upper().str.strip())
        gdf["energy_community"] = gdf["County"].str.upper().str.strip().apply(
            lambda c: "Yes" if c + " COUNTY" in ec_counties or c in ec_counties else "No"
        )
        if (gdf["energy_community"] == "Yes").sum() == 0:
            ec_counties_clean = set(c.replace(" COUNTY", "").strip() for c in ec_counties)
            gdf["energy_community"] = gdf["County"].str.upper().str.strip().apply(
                lambda c: "Yes" if c in ec_counties_clean else "No"
            )
        n_ec = (gdf["energy_community"] == "Yes").sum()
        print(f"  {n_ec}/{len(gdf)} sites in DOE-designated energy communities")
    except Exception as e:
        gdf["energy_community"] = ""
        print(f"  [WARN] Energy community check failed: {e}")
else:
    if "energy_community" not in gdf.columns or gdf["energy_community"].isna().all():
        gdf["energy_community"] = "No"
    print("  [skip] DOE energy community data not found — using enriched values")

# ---------------------------------------------------------------------------
# FEMA NRI composite hazard scoring
# ---------------------------------------------------------------------------
print("\nChecking FEMA National Risk Index …")
nri_path = AL_RAW / "fema_nri" / "nri_al_counties.csv"
if nri_path.exists() and nri_path.stat().st_size > 100:
    nri = pd.read_csv(nri_path)
    nri["_county"] = nri["COUNTY"].str.upper().str.strip()
    nri_risk = nri.set_index("_county")["RISK_SCORE"].to_dict()
    nri_rating = nri.set_index("_county")["RISK_RATNG"].to_dict()
    nri_eq = nri.set_index("_county").get("ERQK_RISKR", pd.Series()).to_dict()
    nri_tornado = nri.set_index("_county").get("TRND_RISKR", pd.Series()).to_dict()
    nri_hurricane = nri.set_index("_county").get("HRCN_RISKR", pd.Series()).to_dict()

    gdf["nri_risk_score"] = gdf["County"].str.upper().str.strip().map(nri_risk)
    gdf["nri_risk_rating"] = gdf["County"].str.upper().str.strip().map(nri_rating).fillna("")
    gdf["nri_earthquake"] = gdf["County"].str.upper().str.strip().map(nri_eq).fillna("")
    gdf["nri_tornado"] = gdf["County"].str.upper().str.strip().map(nri_tornado).fillna("")
    gdf["nri_hurricane"] = gdf["County"].str.upper().str.strip().map(nri_hurricane).fillna("")

    n_mapped = gdf["nri_risk_score"].notna().sum()
    n_high = gdf["nri_risk_rating"].isin(["Relatively High", "Very High"]).sum()
    print(f"  Mapped {n_mapped}/{len(gdf)} sites to NRI risk scores")
    if n_high:
        print(f"  ⚠ {n_high} sites in high/very-high natural hazard risk counties")
else:
    for c in ["nri_risk_score", "nri_risk_rating", "nri_earthquake", "nri_tornado", "nri_hurricane"]:
        gdf[c] = ""
    print("  [skip] FEMA NRI data not found")

# ---------------------------------------------------------------------------
# USGS NSHM — seismic design parameters
# ---------------------------------------------------------------------------
print("\nQuerying USGS seismic hazard (ASCE 7-22) …")

gdf_wgs_seis = gdf.to_crs("EPSG:4326")
seismic_ss = pd.Series(np.nan, index=gdf.index)
seismic_s1 = pd.Series(np.nan, index=gdf.index)
seismic_sdc = pd.Series("", index=gdf.index)

from concurrent.futures import ThreadPoolExecutor as _TPE_seis, as_completed as _ac_seis
import time as _time_seis

def _query_seismic(lat, lon):
    try:
        r = requests.get(
            "https://earthquake.usgs.gov/ws/designmaps/asce7-22.json",
            params={"latitude": round(lat, 4), "longitude": round(lon, 4),
                    "riskCategory": "III", "siteClass": "D", "title": "q"},
            timeout=15, verify=False,
        )
        if r.status_code == 200:
            data = r.json().get("response", {}).get("data", {})
            return data.get("ss"), data.get("s1"), data.get("sdc", "")
    except Exception:
        pass
    return None, None, ""

with _TPE_seis(max_workers=4) as pool:
    futures = {}
    for i in range(len(gdf)):
        pt = gdf_wgs_seis.geometry.iloc[i]
        futures[pool.submit(_query_seismic, pt.y, pt.x)] = i
        _time_seis.sleep(0.1)
    for fut in _ac_seis(futures):
        i = futures[fut]
        ss, s1, sdc = fut.result()
        if ss is not None:
            seismic_ss.iat[i] = ss
            seismic_s1.iat[i] = s1
            seismic_sdc.iat[i] = sdc

gdf["seismic_ss"] = seismic_ss
gdf["seismic_s1"] = seismic_s1
gdf["seismic_sdc"] = seismic_sdc
n_seis = seismic_ss.notna().sum()
print(f"  Queried {n_seis}/{len(gdf)} sites")

gdf["score_seismic"] = gdf["seismic_ss"].apply(score_seismic)
n_high_seis = (gdf["score_seismic"] <= 2.0).sum()
if n_high_seis:
    print(f"  ⚠ {n_high_seis} sites in high seismic hazard zones (score ≤2)")
gdf["infra_score"]  = gdf["infra_score"] + gdf["score_seismic"]
gdf["total_score"]  = gdf["infra_score"] * gdf["score_retirement_mult"]

# ---------------------------------------------------------------------------
# SSURGO soils
# ---------------------------------------------------------------------------
print("\nQuerying SSURGO soils …")

SSURGO_URL = "https://SDMDataAccess.sc.egov.usda.gov/Tabular/post.rest"

def _query_soil(lat, lon):
    query = f"""
    SELECT TOP 1 c.compname, c.drainagecl, c.hydricrating
    FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('POINT({lon} {lat})') mk
    INNER JOIN mapunit mu ON mk.mukey = mu.mukey
    INNER JOIN component c ON mu.mukey = c.mukey
    WHERE c.comppct_r > 10
    ORDER BY c.comppct_r DESC
    """
    try:
        r = requests.post(SSURGO_URL, json={"query": query, "format": "JSON"}, timeout=15)
        if r.status_code == 200:
            rows = r.json().get("Table", [])
            if rows:
                return rows[0]
    except Exception:
        pass
    return None

soil_type = pd.Series("", index=gdf.index)
soil_drainage = pd.Series("", index=gdf.index)
soil_hydric = pd.Series("", index=gdf.index)

with _TPE_seis(max_workers=4) as pool:
    futures = {}
    for i in range(len(gdf)):
        pt = gdf_wgs_seis.geometry.iloc[i]
        futures[pool.submit(_query_soil, pt.y, pt.x)] = i
    for fut in _ac_seis(futures):
        i = futures[fut]
        result = fut.result()
        if result:
            soil_type.iat[i] = str(result[0] or "")
            soil_drainage.iat[i] = str(result[1] or "")
            soil_hydric.iat[i] = str(result[2] or "")

gdf["soil_type"] = soil_type
gdf["soil_drainage"] = soil_drainage
gdf["soil_hydric"] = soil_hydric
n_soil = (soil_type != "").sum()
print(f"  Queried {n_soil}/{len(gdf)} sites")

# ---------------------------------------------------------------------------
# NOAA Storm Events — tornado and severe wind history by county
# ---------------------------------------------------------------------------
print("\nChecking NOAA Storm Events history …")
storm_path = AL_RAW / "noaa_storm_events.csv"
if storm_path.exists():
    storms = pd.read_csv(storm_path, dtype=str)
    storms["_county"] = storms["CZ_NAME"].str.upper().str.strip()
    tornado = storms[storms["EVENT_TYPE"].str.upper().str.contains("TORNADO", na=False)]
    wind = storms[storms["EVENT_TYPE"].str.upper().str.contains("THUNDERSTORM WIND|STRONG WIND", na=False)]

    tornado_count = tornado.groupby("_county").size().to_dict()
    wind_count = wind.groupby("_county").size().to_dict()

    gdf["storm_tornado_count"] = gdf["County"].str.upper().str.strip().map(tornado_count).fillna(0).astype(int)
    gdf["storm_wind_count"] = gdf["County"].str.upper().str.strip().map(wind_count).fillna(0).astype(int)

    n_tornado_high = (gdf["storm_tornado_count"] > 20).sum()
    print(f"  Mapped storm history for {len(tornado_count)} counties")
    print(f"  {n_tornado_high} sites in high-tornado counties (>20 events)")
else:
    gdf["storm_tornado_count"] = 0
    gdf["storm_wind_count"] = 0
    print("  [skip] NOAA storm data not found — run download_westvirginia_extras.py")

# ---------------------------------------------------------------------------
# Placeholder columns for AL sources without a direct equivalent
# (no ERCOT queue; no statewide water-rights / PHMSA diameter lookup wired yet)
# ---------------------------------------------------------------------------
for col in ["ercot_queued_mw", "ercot_n_projects"]:
    if col not in gdf.columns:
        gdf[col] = 0 if "projects" in col else np.nan
# Columns that are populated by enrich_columns.py — only initialise if absent
for col in ["water_basin", "water_permitted_af",
            "pipeline_max_dia_inches", "pipeline_best_operator"]:
    if col not in gdf.columns:
        gdf[col] = ""

# ---------------------------------------------------------------------------
# Rank and select top N
# ---------------------------------------------------------------------------
CONF_SORT_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNVERIFIED": 3, "ACTIVE_WARNING": 4}
if "retirement_confidence" in gdf.columns:
    gdf["_conf_sort"] = gdf["retirement_confidence"].map(CONF_SORT_ORDER).fillna(3)
    gdf = gdf.sort_values(["_conf_sort", "total_score"], ascending=[True, False]).reset_index(drop=True)
    gdf = gdf.drop(columns=["_conf_sort"])
else:
    gdf = gdf.sort_values("total_score", ascending=False).reset_index(drop=True)

gdf.index = gdf.index + 1
gdf.index.name = "rank"

above_threshold = gdf[gdf["total_score"] >= MIN_SCORE]
top = above_threshold.head(TOP_N).copy()
print(f"\n{len(above_threshold)} candidates score ≥{MIN_SCORE}; "
      f"exporting top {len(top)} (cap={TOP_N})")

# ---------------------------------------------------------------------------
# Results table
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("RANKED CANDIDATES — TENNESSEE")
print("=" * 60)

display_cols = [
    "Plant_Name", "State", "County", "brownfield_type",
    "dist_to_pipeline_mi", "pipeline_operator", "nearest_sub_kv",
    "dist_to_sub_mi", "tx_lines_5mi", "nearest_metro", "dist_to_metro_mi",
    "parcel_acres", "total_score",
    "score_gas", "score_substation", "score_tx_redundancy",
    "score_brownfield", "score_state", "score_parcel",
    "score_water", "score_metro", "score_utility_rate", "score_air_permit",
    "score_fiber", "fiber_max_speed_mbps",
    "score_opportunity_zone", "opportunity_zone",
    "score_flood_risk", "flood_zone",
]
display_cols = [c for c in display_cols if c in gdf.columns]

pd.set_option("display.max_columns", 20)
pd.set_option("display.width", 160)
pd.set_option("display.float_format", "{:.1f}".format)

print(top[display_cols].to_string())

print("\n--- Score breakdown ---")
print(f"{'Rank':<5} {'Plant':<35} {'Score':>6}  "
      f"{'Gas':>5} {'Sub':>5} {'TxN':>4} {'BF':>4} {'St':>4} "
      f"{'H2O':>4} {'Metro':>5} {'Rate':>5} {'Prcl':>5} {'Air':>4} {'Fbr':>4} {'Acres':>7}  {'PipeDist':>9} {'SubKV':>6} {'Lines':>6}")
print("-" * 133)
for rank, row in top[display_cols].iterrows():
    acres    = f"{row['parcel_acres']:.0f}"           if pd.notna(row.get("parcel_acres"))         else "n/a"
    pdist    = f"{row['dist_to_pipeline_mi']:.1f}mi"  if pd.notna(row.get("dist_to_pipeline_mi"))  else "n/a"
    sub_kv   = f"{row['nearest_sub_kv']:.0f}kV"       if pd.notna(row.get("nearest_sub_kv"))       else "n/a"
    lines    = str(int(row.get("tx_lines_5mi", 0)))
    print(
        f"{rank:<5} {str(row['Plant_Name']):<35} {row['total_score']:>6.1f}  "
        f"{row.get('score_gas',0):>5.1f} {row.get('score_substation',0):>5.1f} "
        f"{row.get('score_tx_redundancy',0):>4.1f} {row.get('score_brownfield',0):>4.0f} "
        f"{row.get('score_state',0):>4.0f} "
        f"{row.get('score_water',0):>4.0f} {row.get('score_metro',0):>5.1f} "
        f"{row.get('score_utility_rate',0):>5.1f} {row.get('score_parcel',0):>5.1f} "
        f"{row.get('score_air_permit',0):>4.0f} {row.get('score_fiber',0):>4.0f} {acres:>7}  "
        f"{pdist:>9} {sub_kv:>6} {lines:>6}"
    )

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
print("\nExporting outputs …")

EXPORT_COLS = [
    "rank",
    "Plant_Name", "Street_Address", "City", "County", "State",
    "Latitude", "Longitude",
    "source", "site_id", "brownfield_type", "naics_code", "total_mw",
    "current_owner", "owner_source", "franchise_tax_status",
    "nearest_sub_kv", "dist_to_sub_mi", "tx_lines_5mi",
    "sub_capacity_mva_est", "grid_capacity_mw_est",
    "county_generation_mw", "transmission_utility",
    "dist_to_pipeline_mi", "pipeline_operator", "pipeline_type",
    "pipeline_dia_est", "pipeline_pressure_psi",
    "pipeline_operators_5mi", "pipeline_max_dia_inches", "pipeline_best_operator",
    "dist_to_water_mi", "water_basin", "water_permitted_af",
    "parcel_acres", "cad_acres", "osm_acres",
    "retirement_confidence", "retirement_signals",
    "nearest_metro", "dist_to_metro_mi",
    "energy_community",
    "nri_risk_score", "nri_risk_rating", "nri_earthquake", "nri_tornado", "nri_hurricane",
    "wetland_overlap", "protected_area_nearby",
    "seismic_ss", "seismic_s1", "seismic_sdc",
    "soil_type", "soil_drainage", "soil_hydric",
    "storm_tornado_count", "storm_wind_count",
    "fiber_max_speed_mbps",
    "has_air_permit",
    "total_score",
    "score_gas", "score_substation", "score_tx_redundancy",
    "score_brownfield", "score_state",
    "score_water", "score_metro", "score_utility_rate", "score_parcel",
    "score_air_permit", "score_fiber", "score_opportunity_zone", "score_flood_risk",
    "score_seismic",
    "opportunity_zone", "flood_zone", "eia860_retired",
    "infra_score", "score_retirement_mult",
]
export_df = top.reset_index()
export_cols_present = [c for c in EXPORT_COLS if c in export_df.columns]

csv_path = OUT_CSV / f"top_candidates_{STATE}.csv"
export_df[export_cols_present].to_csv(csv_path, index=False)
print(f"  CSV:     {csv_path}")

gj_path = OUT_GJ / f"top_candidates_{STATE}.geojson"
gj_df = top.reset_index().to_crs("EPSG:4326")
gj_cols_present = [c for c in export_cols_present if c != "rank"] + ["geometry"]
gj_df[[c for c in gj_cols_present if c in gj_df.columns]].to_file(gj_path, driver="GeoJSON")
print(f"  GeoJSON: {gj_path}")

print(f"\nDone. {len(top)} West Virginia sites exported.")
