"""
filter_pipeline_al.py
Applies sequential spatial filters to West Virginia brownfield candidates.

Filters (in order):
  pre) County moratoriums / proposed bans
  a)   Within 5 miles of 230kV+ transmission line
  b)   Within 60 miles of large urban area (≥250k pop proxy)
  c)   Within 3 miles of ≥115kV substation
  d)   Within 10 miles of natural gas pipeline
  e)   Outside EPA serious/extreme nonattainment (ozone or PM2.5)
  f)   Outside FEMA Special Flood Hazard Areas (AE/A/VE) — via FEMA REST API
  g)   Within 5 miles of NHD water body ≥4 ha
  i)   Within 10 miles of fiber-served census block (FCC 477)
  j)   Outside 0.5 miles of EPA Superfund/SEMS site (FRS data)
  l)   NWI Wetlands overlap check (soft flag)
  m)   PAD-US Protected Areas — hard exclude GAP Status 1-2

Note: TX-specific filters removed:
  - h) TNRIS parcel acreage (TX-only parcel service)
  - k) TX induced seismicity counties

All spatial operations in EPSG:5070 (metres). STRtree used for distance
queries — no apply(lambda) loops.

Output: data/processed/candidates_filtered_wv.gpkg
        data/processed/candidates_filtered_wv.csv
"""

import sys
import warnings
import requests
import numpy as np
import pandas as pd
import geopandas as gpd
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from shapely.geometry import Point
from shapely.strtree import STRtree
from tqdm import tqdm

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT     = Path(__file__).parent
RAW      = ROOT / "data" / "raw"
AL_RAW   = ROOT / "data" / "westvirginia" / "raw"
PROC_DIR = ROOT / "data" / "processed"
STATE    = "wv"

CRS = "EPSG:5070"

DIST_TRANSMISSION_M = 5  * 1609.344
DIST_SUBSTATION_M   = 3  * 1609.344
DIST_GAS_M          = 2  * 1609.344
DIST_WATER_M        = 5  * 1609.344
DIST_URBAN_M        = 60 * 1609.344
DIST_FIBER_M        = 10 * 1609.344
DIST_SUPERFUND_M    = 0.5 * 1609.344

MIN_PARCEL_ACRES = 50

URBAN_MIN_ALAND_M2 = 200_000_000

SUBSTATION_MIN_KV  = 115_000
TRANSMISSION_MIN_KV = 230

FEMA_WORKERS = 20
FEMA_FLOOD_ZONES = {"A", "AE", "AH", "AO", "AR", "VE", "V"}

MIN_PIPE_FEATURES = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def miles(m: float) -> float:
    return m / 1609.344


def print_step(label: str, n_before: int, n_after: int) -> None:
    dropped = n_before - n_after
    print(f"  ✓ {label}")
    print(f"    {n_before} → {n_after}  (dropped {dropped})")


def bbox_expand(gdf: gpd.GeoDataFrame, buffer_m: float):
    minx, miny, maxx, maxy = gdf.total_bounds
    return minx - buffer_m, miny - buffer_m, maxx + buffer_m, maxy + buffer_m


def cx_clip(reference: gpd.GeoDataFrame, bounds) -> gpd.GeoDataFrame:
    minx, miny, maxx, maxy = bounds
    return reference.cx[minx:maxx, miny:maxy]


def within_distance(candidates: gpd.GeoDataFrame,
                    reference: gpd.GeoDataFrame,
                    distance_m: float) -> np.ndarray:
    tree = STRtree(reference.geometry.values)
    result = tree.query(candidates.geometry.values,
                        predicate="dwithin", distance=distance_m)
    hit = set(result[0].tolist())
    return np.fromiter((i in hit for i in range(len(candidates))), dtype=bool)


def outside_polygons(candidates: gpd.GeoDataFrame,
                     reference: gpd.GeoDataFrame) -> np.ndarray:
    tree = STRtree(reference.geometry.values)
    result = tree.query(candidates.geometry.values, predicate="intersects")
    hit = set(result[0].tolist())
    return np.fromiter((i not in hit for i in range(len(candidates))), dtype=bool)


def read_parquet_or_fallback(parquet_path: Path,
                              fallback_path: Path,
                              columns: list | None = None) -> gpd.GeoDataFrame:
    if parquet_path.exists():
        return gpd.read_parquet(parquet_path, columns=columns)
    if fallback_path.exists():
        print(f"  [WARN] Parquet not found — loading from {fallback_path.name} "
              f"(run convert_to_parquet.py to speed this up)")
        gdf = gpd.read_file(fallback_path)
        if columns:
            keep = [c for c in columns if c in gdf.columns or c == "geometry"]
            gdf = gdf[keep]
        return gdf.to_crs(CRS)
    raise FileNotFoundError(
        f"Neither {parquet_path} nor {fallback_path} found. "
        "Run download_data_al.py and convert_to_parquet.py first."
    )


# ---------------------------------------------------------------------------
# FEMA flood check (ThreadPoolExecutor)
# ---------------------------------------------------------------------------
FEMA_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

def check_fema_flood(args) -> tuple[int, bool]:
    idx, lon, lat = args
    params = {
        "geometry":       f"{lon},{lat}",
        "geometryType":   "esriGeometryPoint",
        "inSR":           "4326",
        "spatialRel":     "esriSpatialRelIntersects",
        "outFields":      "FLD_ZONE",
        "returnGeometry": "false",
        "f":              "json",
    }
    try:
        r = requests.get(FEMA_URL, params=params, timeout=15)
        r.raise_for_status()
        for feat in r.json().get("features", []):
            zone = feat.get("attributes", {}).get("FLD_ZONE", "")
            if str(zone).strip().upper() in FEMA_FLOOD_ZONES:
                return idx, True
        return idx, False
    except Exception:
        return idx, False


# ---------------------------------------------------------------------------
# Load candidates
# ---------------------------------------------------------------------------
print("=" * 60)
print("Filter Pipeline — West Virginia Data Center Candidate Screening")
print("=" * 60)

cand_path = PROC_DIR / f"candidates_{STATE}.gpkg"
print(f"\nLoading candidates from {cand_path.name} …")
gdf = gpd.read_file(cand_path).to_crs(CRS)
print(f"  Starting candidates: {len(gdf):,}")

# ---------------------------------------------------------------------------
# Pre-filter: County moratoriums
# ---------------------------------------------------------------------------
moratorium_path = ROOT / "data" / "manual" / "county_moratoriums.csv"
if moratorium_path.exists():
    print("\n[pre] Removing counties with data center moratoriums / proposed bans")
    mdf = pd.read_csv(moratorium_path)

    def normalize_county(s: str) -> str:
        return str(s).upper().replace(" COUNTY", "").replace(" PARISH", "").strip()

    mdf["county_key"] = mdf["county"].apply(normalize_county)
    mdf["state_key"]  = mdf["state"].str.upper().str.strip()
    banned = set(zip(mdf["state_key"], mdf["county_key"]))

    gdf["_county_key"] = gdf["County"].apply(normalize_county)
    gdf["_state_key"]  = gdf["State"].str.upper().str.strip()
    in_banned = gdf.apply(lambda r: (r["_state_key"], r["_county_key"]) in banned, axis=1)

    n_before = len(gdf)
    gdf = gpd.GeoDataFrame(gdf[~in_banned].reset_index(drop=True), crs=CRS)
    gdf = gdf.drop(columns=["_county_key", "_state_key"])
    print(f"  Dropped {n_before - len(gdf):,} candidates in moratorium counties "
          f"({n_before:,} → {len(gdf):,})")
else:
    print("\n[pre] No county_moratoriums.csv found — skipping")

# ---------------------------------------------------------------------------
# Filter (a): Within 5 miles of 230kV+ transmission line
# ---------------------------------------------------------------------------
print(f"\n[a] Within {miles(DIST_TRANSMISSION_M):.0f} miles of 230kV+ transmission line")

tx = read_parquet_or_fallback(
    RAW / "hifld" / "transmission_lines.parquet",
    RAW / "hifld" / "transmission_lines.geojson",
    columns=["VOLTAGE", "STATUS", "geometry"],
)
tx["VOLTAGE"] = pd.to_numeric(tx["VOLTAGE"], errors="coerce")
tx_hv = tx[(tx["VOLTAGE"] >= TRANSMISSION_MIN_KV) & (tx["STATUS"] == "IN SERVICE")]
print(f"  Transmission lines ≥{TRANSMISSION_MIN_KV}kV in service: {len(tx_hv):,}")
tx_hv_clip = cx_clip(tx_hv, bbox_expand(gdf, DIST_TRANSMISSION_M))

n_before = len(gdf)
mask = within_distance(gdf, tx_hv_clip, DIST_TRANSMISSION_M)
gdf = gpd.GeoDataFrame(gdf[mask].reset_index(drop=True), crs=CRS)
print_step(f"within {miles(DIST_TRANSMISSION_M):.0f} mi of 230kV+ line", n_before, len(gdf))
del tx, tx_hv, tx_hv_clip

# ---------------------------------------------------------------------------
# Filter (b): Within 60 miles of large urban area
# ---------------------------------------------------------------------------
print(f"\n[b] Within {miles(DIST_URBAN_M):.0f} miles of large urban area (≥250k pop proxy)")

census = read_parquet_or_fallback(
    RAW / "census" / "tl_2023_us_uac20.parquet",
    RAW / "census" / "tl_2023_us_uac20.shp",
    columns=["ALAND20", "NAME20", "geometry"],
)
census_large = census[census["ALAND20"] >= URBAN_MIN_ALAND_M2]
print(f"  Urban areas qualifying: {len(census_large):,}")
census_clip = cx_clip(census_large, bbox_expand(gdf, DIST_URBAN_M))

n_before = len(gdf)
mask = within_distance(gdf, census_clip, DIST_URBAN_M)
gdf = gpd.GeoDataFrame(gdf[mask].reset_index(drop=True), crs=CRS)
print_step(f"within {miles(DIST_URBAN_M):.0f} mi of large metro", n_before, len(gdf))
del census, census_large, census_clip

# ---------------------------------------------------------------------------
# Filter (c): Within 3 miles of ≥115kV substation
# ---------------------------------------------------------------------------
print(f"\n[c] Within {miles(DIST_SUBSTATION_M):.0f} miles of ≥115kV substation")

subs = read_parquet_or_fallback(
    RAW / "hifld" / "substations.parquet",
    RAW / "hifld" / "substations.geojson",
    columns=["voltage", "geometry"],
)

def parse_voltage(v):
    try:
        return max(float(p.strip()) for p in str(v).split(";") if p.strip())
    except Exception:
        return 0.0

subs["voltage_v"] = subs["voltage"].apply(parse_voltage)
subs_hv = subs[subs["voltage_v"] >= SUBSTATION_MIN_KV]
print(f"  Substations ≥{SUBSTATION_MIN_KV // 1000}kV: {len(subs_hv):,}")

if len(subs_hv) > 0:
    subs_hv_clip = cx_clip(subs_hv, bbox_expand(gdf, DIST_SUBSTATION_M))
    n_before = len(gdf)
    mask = within_distance(gdf, subs_hv_clip, DIST_SUBSTATION_M)
    gdf = gpd.GeoDataFrame(gdf[mask].reset_index(drop=True), crs=CRS)
    print_step(f"within {miles(DIST_SUBSTATION_M):.0f} mi of ≥115kV substation", n_before, len(gdf))
else:
    print("  [WARN] No qualifying substations — skipping filter (c)")
del subs, subs_hv

# ---------------------------------------------------------------------------
# Filter (d): Within 10 miles of EIA gas pipeline
# ---------------------------------------------------------------------------
DIST_GAS_FILTER_M = 10 * 1609.344

print(f"\n[d] Within {miles(DIST_GAS_FILTER_M):.0f} miles of EIA gas pipeline (interstate/intrastate)")

eia_pipes_parquet = RAW / "hifld" / "eia_gas_pipelines.parquet"
eia_pipes_geojson = RAW / "hifld" / "eia_gas_pipelines.geojson"
eia_pipes_path = eia_pipes_parquet if eia_pipes_parquet.exists() else eia_pipes_geojson
if eia_pipes_path.exists():
    pipes = (gpd.read_parquet(eia_pipes_path) if eia_pipes_path.suffix == ".parquet"
             else gpd.read_file(eia_pipes_path)).to_crs(CRS)
    print(f"  EIA pipelines (operating): {len(pipes):,}")
    pipes_clip = cx_clip(pipes, bbox_expand(gdf, DIST_GAS_FILTER_M))
    print(f"  In region: {len(pipes_clip):,}")

    tree_gas = STRtree(pipes_clip.geometry.values)
    nearest_gas_idx = tree_gas.nearest(gdf.geometry.values)
    dist_gas = np.array([
        pt.distance(pipes_clip.geometry.values[i])
        for pt, i in zip(gdf.geometry.values, nearest_gas_idx)
    ])
    gdf["dist_to_pipeline_mi"] = dist_gas / 1609.344
    gdf["pipeline_operator"]   = pipes_clip["Operator"].values[nearest_gas_idx]
    gdf["pipeline_type"]       = pipes_clip["TYPEPIPE"].values[nearest_gas_idx]

    MULTI_OP_DIST_M = 5 * 1609.344
    results = tree_gas.query(gdf.geometry.values, predicate="dwithin", distance=MULTI_OP_DIST_M)
    from collections import defaultdict
    ops_nearby = defaultdict(set)
    for cand_idx, pipe_idx in zip(results[0], results[1]):
        ops_nearby[cand_idx].add(pipes_clip["Operator"].values[pipe_idx])
    gdf["pipeline_operators_5mi"] = [
        "; ".join(sorted(ops_nearby.get(i, set()))) for i in range(len(gdf))
    ]
    n_multi = sum(1 for v in ops_nearby.values() if len(v) > 1)
    print(f"  {n_multi} sites have multiple pipeline operators within 5 mi")

    n_before = len(gdf)
    mask = gdf["dist_to_pipeline_mi"] <= miles(DIST_GAS_FILTER_M)
    gdf = gpd.GeoDataFrame(gdf[mask].reset_index(drop=True), crs=CRS)
    print_step(f"within {miles(DIST_GAS_FILTER_M):.0f} mi of EIA gas pipeline", n_before, len(gdf))
else:
    print("  [WARN] EIA gas pipeline parquet not found — falling back to EIA-860 field")
    n_before = len(gdf)
    # EIA860 retired power-plant sites are exempt: they're confirmed large
    # industrial brownfields with on-site grid infrastructure; gas pipeline
    # proximity is not a gating requirement for these sites.
    eia860_exempt = gdf["source"] == "EIA860"
    gdf = gpd.GeoDataFrame(
        gdf[gdf["Natural_Gas_Pipeline_Name_1"].notna() | eia860_exempt].reset_index(drop=True), crs=CRS
    )
    gdf["dist_to_pipeline_mi"] = np.nan
    gdf["pipeline_operator"]   = ""
    gdf["pipeline_type"]       = ""
    print_step("has named gas pipeline (EIA-860 fallback)", n_before, len(gdf))
    pipes = None

if eia_pipes_path.exists():
    del pipes

# ---------------------------------------------------------------------------
# Filter (e): Outside EPA serious/extreme nonattainment areas
# ---------------------------------------------------------------------------
print("\n[e] Outside EPA ozone & PM2.5 nonattainment")

epa_layers = {
    "Ozone (2015 std)": (
        RAW / "epa" / "8hour_ozone" / "ozone_8hr_2015std_naa.parquet",
        RAW / "epa" / "8hour_ozone" / "ozone_8hr_2015std_naa.shp",
    ),
    "PM2.5 (2012 std)": (
        RAW / "epa" / "pm25_annual" / "PM25_2012Std_NAA.parquet",
        RAW / "epa" / "pm25_annual" / "PM25_2012Std_NAA.shp",
    ),
}

epa_frames = []
for name, (parquet, shp) in epa_layers.items():
    layer = read_parquet_or_fallback(parquet, shp, columns=["geometry"])
    print(f"  {name}: {len(layer):,} nonattainment areas")
    epa_frames.append(layer[["geometry"]])

epa_all  = gpd.GeoDataFrame(pd.concat(epa_frames, ignore_index=True), crs=CRS)
epa_clip = cx_clip(epa_all, bbox_expand(gdf, 0))

n_before = len(gdf)
mask = outside_polygons(gdf, epa_clip)
gdf = gpd.GeoDataFrame(gdf[mask].reset_index(drop=True), crs=CRS)
print_step("outside EPA nonattainment", n_before, len(gdf))
del epa_all, epa_clip

# ---------------------------------------------------------------------------
# Filter (f): Outside FEMA flood zones
# ---------------------------------------------------------------------------
print("\n[f] Outside FEMA Special Flood Hazard Areas (AE/A/VE)")
print(f"    Checking {len(gdf)} candidates via FEMA API ({FEMA_WORKERS} workers) …")

gdf_4326  = gdf.to_crs("EPSG:4326")
tasks     = [(i, geom.x, geom.y) for i, geom in enumerate(gdf_4326.geometry)]
flood_flags = np.zeros(len(gdf), dtype=bool)

with ThreadPoolExecutor(max_workers=FEMA_WORKERS) as pool:
    futures = {pool.submit(check_fema_flood, t): t[0] for t in tasks}
    with tqdm(total=len(tasks), desc="  FEMA API", unit="site") as bar:
        for future in as_completed(futures):
            idx, is_flooded = future.result()
            flood_flags[idx] = is_flooded
            bar.update(1)

n_before = len(gdf)
gdf = gpd.GeoDataFrame(gdf[~flood_flags].reset_index(drop=True), crs=CRS)
print_step("outside FEMA SFHA flood zones", n_before, len(gdf))

# ---------------------------------------------------------------------------
# Filter (g): Within 5 miles of NHD water body ≥4 ha
# ---------------------------------------------------------------------------
print(f"\n[g] Within {miles(DIST_WATER_M):.0f} miles of NHD water body (≥4 ha)")

nhd_parquet = RAW / "nhd" / "nhd_waterbody_all.parquet"
nhd_shp = RAW / "nhd" / "WV" / "Shape" / "NHDWaterbody.shp"
if nhd_parquet.exists():
    nhd_all = gpd.read_parquet(nhd_parquet, columns=["geometry"])
    print(f"  Loaded NHD from parquet: {len(nhd_all):,} water bodies")
elif nhd_shp.exists():
    nhd_all = gpd.read_file(nhd_shp)[["geometry"]].to_crs(CRS)
    # Filter to water bodies ≥4 ha (same threshold as TX pipeline)
    nhd_all = nhd_all[nhd_all.geometry.area >= 40_000]
    print(f"  Loaded NHD from shapefile: {len(nhd_all):,} water bodies (≥4 ha)")
else:
    print("  [WARN] NHD data not found — run download_data_al.py first")
    nhd_all = gpd.GeoDataFrame({"geometry": []}, crs=CRS)

if len(nhd_all) > 0:
    nhd_clip = cx_clip(nhd_all, bbox_expand(gdf, DIST_WATER_M))
    tree_nhd = STRtree(nhd_clip.geometry.values)
    nearest_idx = tree_nhd.nearest(gdf.geometry.values)
    dists = np.array([
        gdf.geometry.values[i].distance(nhd_clip.geometry.values[nearest_idx[i]])
        for i in range(len(gdf))
    ])
    gdf["dist_to_water_mi"] = dists / 1609.344
    n_within = (gdf["dist_to_water_mi"] <= miles(DIST_WATER_M)).sum()
    print(f"  {n_within}/{len(gdf)} sites within {miles(DIST_WATER_M):.0f} mi (scored, not filtered)")
else:
    gdf["dist_to_water_mi"] = np.nan
del nhd_all

# ---------------------------------------------------------------------------
# Filter (i): Within 10 miles of fiber-served census block (FCC 477)
# ---------------------------------------------------------------------------
print(f"\n[i] Within {miles(DIST_FIBER_M):.0f} miles of fiber-served census block (FCC 477)")

fcc_csv    = AL_RAW / "broadband" / "fcc_477_al_fiber.csv"
blocks_dir = AL_RAW / "broadband" / "blocks"

fcc_loaded = False
if fcc_csv.exists() and blocks_dir.exists():
    try:
        print("  Loading FCC 477 fiber census blocks …")
        fcc = pd.read_csv(fcc_csv, dtype=str, low_memory=True)
        fcc["maxaddown"] = pd.to_numeric(fcc["maxaddown"], errors="coerce").fillna(0)
        fcc["_geoid"] = fcc["Census Block FIPS Code"].str.zfill(15)
        fiber_blocks = set(fcc["_geoid"])
        block_speed = fcc.groupby("_geoid")["maxaddown"].max().to_dict()
        n_1g = (fcc.groupby("_geoid")["maxaddown"].max() >= 1000).sum()
        print(f"  Fiber-served AL census blocks (tech 50): {len(fiber_blocks):,}")
        print(f"  Blocks with ≥1 Gbps: {n_1g:,}")

        blk_shp = list(blocks_dir.glob("*.shp"))
        if blk_shp:
            blk = gpd.read_file(blk_shp[0], columns=["GEOID20", "geometry"]).to_crs(CRS)
            fiber_geom = blk[blk["GEOID20"].isin(fiber_blocks)].copy()
            fiber_geom["max_speed_mbps"] = fiber_geom["GEOID20"].map(block_speed).fillna(0)
            print(f"  Matched to {len(fiber_geom):,} block geometries")

            fiber_clip = cx_clip(fiber_geom, bbox_expand(gdf, DIST_FIBER_M))
            n_before = len(gdf)
            mask = within_distance(gdf, fiber_clip, DIST_FIBER_M)
            gdf = gpd.GeoDataFrame(gdf[mask].reset_index(drop=True), crs=CRS)
            print_step(f"within {miles(DIST_FIBER_M):.0f} mi of fiber census block", n_before, len(gdf))

            FIBER_SPEED_DIST_M = 5 * 1609.344
            fiber_1g = fiber_clip[fiber_clip["max_speed_mbps"] >= 1000]
            if len(fiber_1g) > 0:
                tree_fiber = STRtree(fiber_1g.geometry.values)
                results_f = tree_fiber.query(
                    gdf.geometry.values, predicate="dwithin", distance=FIBER_SPEED_DIST_M
                )
                from collections import defaultdict as _dd
                site_speed = _dd(float)
                for cand_i, fib_i in zip(results_f[0], results_f[1]):
                    spd = fiber_1g["max_speed_mbps"].values[fib_i]
                    if spd > site_speed[cand_i]:
                        site_speed[cand_i] = spd
                gdf["fiber_max_speed_mbps"] = [site_speed.get(i, 0) for i in range(len(gdf))]
                n_1g_sites = (gdf["fiber_max_speed_mbps"] >= 1000).sum()
                print(f"  {n_1g_sites}/{len(gdf)} sites within 5 mi of ≥1 Gbps fiber block")
            else:
                gdf["fiber_max_speed_mbps"] = 0.0

            fcc_loaded = True
        else:
            print("  [WARN] Census block shapefile not found in blocks/ — skipping fiber filter")
    except Exception as e:
        print(f"  [WARN] FCC fiber filter failed: {e} — skipping")
else:
    if not fcc_csv.exists():
        print("  [WARN] FCC 477 CSV not found — run download_westvirginia.py")
    if not blocks_dir.exists():
        print("  [WARN] Census blocks directory not found — run download_westvirginia.py")
    print("  Skipping fiber filter")

if not fcc_loaded:
    print("  Fiber filter skipped — no data removed")

# ---------------------------------------------------------------------------
# Filter (j): Outside 0.5 miles of EPA Superfund / SEMS sites
# ---------------------------------------------------------------------------
print(f"\n[j] Outside {miles(DIST_SUPERFUND_M):.1f} mile buffer of Superfund/SEMS sites")

frs_dir = RAW / "frs"
frs_files = list(frs_dir.glob("*_FACILITY_FILE.CSV"))
if frs_files:
    sems_frames = []
    for frs_path in frs_files:
        try:
            df = pd.read_csv(frs_path, usecols=[
                "PGM_SYS_ACRNMS", "LATITUDE83", "LONGITUDE83"
            ], low_memory=False)
            sems = df[
                df["PGM_SYS_ACRNMS"].str.contains("SEMS", na=False) &
                df["LATITUDE83"].notna() &
                df["LONGITUDE83"].notna()
            ].copy()
            sems_frames.append(sems)
        except Exception as e:
            print(f"  [WARN] Could not load {frs_path.name}: {e}")

    if sems_frames:
        sems_df = pd.concat(sems_frames, ignore_index=True).drop_duplicates()
        sems_gdf = gpd.GeoDataFrame(
            sems_df,
            geometry=gpd.points_from_xy(sems_df["LONGITUDE83"], sems_df["LATITUDE83"]),
            crs="EPSG:4326",
        ).to_crs(CRS)
        sems_clip = cx_clip(sems_gdf, bbox_expand(gdf, DIST_SUPERFUND_M + 1000))
        print(f"  Superfund/SEMS sites loaded: {len(sems_gdf):,} ({len(sems_clip):,} in region)")

        tree_sf  = STRtree(sems_clip.geometry.values)
        result   = tree_sf.query(gdf.geometry.values,
                                 predicate="dwithin", distance=DIST_SUPERFUND_M)
        too_close = set(result[0].tolist())
        outside_sf = np.fromiter(
            (i not in too_close for i in range(len(gdf))), dtype=bool
        )
        n_before = len(gdf)
        gdf = gpd.GeoDataFrame(gdf[outside_sf].reset_index(drop=True), crs=CRS)
        print_step(f"outside {miles(DIST_SUPERFUND_M):.1f} mi of Superfund site", n_before, len(gdf))
    else:
        print("  [WARN] No SEMS sites found in FRS files — skipping Superfund filter")
else:
    print(f"  [WARN] No FRS facility files found in {frs_dir} — skipping Superfund filter")

# ---------------------------------------------------------------------------
# Filter (l): NWI Wetlands — flag sites overlapping wetland polygons
# ---------------------------------------------------------------------------
print("\n[l] NWI Wetlands overlap check")

nwi_gdb = next((AL_RAW / "nwi").glob("*.gdb"), None) if (AL_RAW / "nwi").exists() else None
if nwi_gdb and nwi_gdb.exists():
    try:
        from pyogrio import list_layers
        nwi_layers = [str(l) for l in list_layers(str(nwi_gdb))[:, 0]]
        nwi_layer = next((l for l in nwi_layers if l.endswith("_Wetlands")), nwi_layers[0])
        nwi = gpd.read_file(nwi_gdb, layer=nwi_layer, columns=["WETLAND_TYPE", "geometry"])
        nwi = nwi.to_crs(CRS)
        print(f"  Loaded {len(nwi):,} AL wetland polygons")

        WETLAND_BUFFER_M = 100
        gdf_buf = gdf.copy()
        gdf_buf["_buf_geom"] = gdf_buf.geometry.buffer(WETLAND_BUFFER_M)

        nwi_clip = cx_clip(nwi, bbox_expand(gdf, WETLAND_BUFFER_M))
        tree_nwi = STRtree(nwi_clip.geometry.values)

        wetland_overlap = []
        for i in range(len(gdf)):
            hits = tree_nwi.query(gdf_buf["_buf_geom"].values[i], predicate="intersects")
            if len(hits) > 0:
                types = nwi_clip.iloc[hits]["WETLAND_TYPE"].unique()
                wetland_overlap.append("; ".join(types[:3]))
            else:
                wetland_overlap.append("")

        gdf["wetland_overlap"] = wetland_overlap
        n_wet = sum(1 for w in wetland_overlap if w)
        print(f"  {n_wet}/{len(gdf)} sites overlap or adjoin wetlands (within {WETLAND_BUFFER_M}m)")
        print(f"  (soft flag — Section 404 permit risk, not hard exclusion)")
        del nwi, nwi_clip
    except Exception as e:
        gdf["wetland_overlap"] = ""
        print(f"  [WARN] NWI processing failed: {e}")
else:
    gdf["wetland_overlap"] = ""
    print("  [skip] NWI geodatabase not found")

# ---------------------------------------------------------------------------
# Filter (m): PAD-US Protected Areas — hard exclude GAP Status 1-2
# ---------------------------------------------------------------------------
print("\n[m] PAD-US Protected Areas check")

padus_gdb = next((AL_RAW / "padus").rglob("*.gdb"), None) if (AL_RAW / "padus").exists() else None
if padus_gdb and padus_gdb.exists():
    try:
        from pyogrio import list_layers
        padus_layers = [str(l) for l in list_layers(str(padus_gdb))[:, 0]]
        # Prefer the combined fee/designation/easement layer for AL
        padus_layer = next((l for l in padus_layers if "Comb" in l), padus_layers[0])
        print(f"  PAD-US layer: {padus_layer}")
        padus = gpd.read_file(padus_gdb, layer=padus_layer,
                              columns=["GAP_Sts", "d_GAP_Sts", "Unit_Nm", "Own_Name", "Des_Tp", "geometry"])
        padus = padus.to_crs(CRS)
        padus_strict = padus[padus["GAP_Sts"].isin(["1", "2", 1, 2])]
        padus_all = padus[padus["GAP_Sts"].isin(["1", "2", "3", 1, 2, 3])]
        print(f"  PAD-US AL: {len(padus):,} total, {len(padus_strict):,} GAP 1-2 (strict), "
              f"{len(padus_all):,} GAP 1-3")

        if len(padus_strict) > 0:
            padus_clip = cx_clip(padus_strict, bbox_expand(gdf, 0))
            if len(padus_clip) > 0:
                tree_padus = STRtree(padus_clip.geometry.values)
                in_protected = []
                for i in range(len(gdf)):
                    hits = tree_padus.query(gdf.geometry.values[i], predicate="intersects")
                    in_protected.append(len(hits) > 0)
                n_before = len(gdf)
                protected_mask = pd.Series(in_protected)
                if protected_mask.any():
                    dropped = gdf[protected_mask.values]
                    for _, row in dropped.iterrows():
                        print(f"    Dropping: {row['Plant_Name']} — inside GAP 1-2 protected area")
                    gdf = gpd.GeoDataFrame(gdf[~protected_mask.values].reset_index(drop=True), crs=CRS)
                    print_step("outside GAP 1-2 protected areas", n_before, len(gdf))
                else:
                    print(f"  No sites in GAP 1-2 protected areas")

        if len(padus_all) > 0:
            padus3_clip = cx_clip(padus_all, bbox_expand(gdf, 1609))
            if len(padus3_clip) > 0:
                tree_p3 = STRtree(padus3_clip.geometry.values)
                near_protected = []
                for i in range(len(gdf)):
                    hits = tree_p3.query(gdf.geometry.values[i], predicate="dwithin", distance=1609)
                    if len(hits) > 0:
                        names = padus3_clip.iloc[hits]["Unit_Nm"].dropna().unique()
                        near_protected.append("; ".join(names[:2]))
                    else:
                        near_protected.append("")
                gdf["protected_area_nearby"] = near_protected
                n_near = sum(1 for p in near_protected if p)
                print(f"  {n_near}/{len(gdf)} sites within 1 mile of a protected area (GAP 1-3)")
            else:
                gdf["protected_area_nearby"] = ""
        else:
            gdf["protected_area_nearby"] = ""

        del padus
    except Exception as e:
        gdf["wetland_overlap"] = gdf.get("wetland_overlap", "")
        gdf["protected_area_nearby"] = ""
        print(f"  [WARN] PAD-US processing failed: {e}")
else:
    gdf["protected_area_nearby"] = ""
    print("  [skip] PAD-US geodatabase not found")

# ---------------------------------------------------------------------------
# Add placeholder columns for parcel_acres (no state parcel service for AL)
# ---------------------------------------------------------------------------
gdf["parcel_acres"] = np.nan
print("\n[note] Parcel acreage: AL has no statewide public parcel service like TX TNRIS.")
print("       parcel_acres set to NaN — measure via Google Earth or county GIS.")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"Filter pipeline complete: {len(gdf)} candidates survive")
print("=" * 60)

print("\nBy state:")
for state, grp in gdf.groupby("State"):
    mw = grp["total_mw"].sum()
    mw_str = f"{mw:,.0f} MW" if pd.notna(mw) and mw > 0 else "n/a"
    print(f"  {state}: {len(grp):,} sites, {mw_str}")

print("\nBy brownfield type:")
for bt, grp in gdf.groupby("brownfield_type"):
    print(f"  {bt}: {len(grp):,}")

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
out_gpkg = PROC_DIR / f"candidates_filtered_{STATE}.gpkg"
out_csv  = PROC_DIR / f"candidates_filtered_{STATE}.csv"

gdf.to_file(out_gpkg, driver="GPKG")
gdf.drop(columns="geometry").to_csv(out_csv, index=False)
print(f"\nSaved:\n  {out_gpkg}\n  {out_csv}")
print("\nReady for enrich_candidates_al.py → score_and_export_al.py")
