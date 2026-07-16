"""
build_candidates_al.py
Builds the candidate site pool for West Virginia from six sources:
  1. EIA Form 860 retired generators (power plants ≥50 MW)
  2. EPA FRS manufacturing facilities (paper, steel, chemicals, refineries)
  3. EPA TRI closed facilities
  4. OSM industrial landuse polygons (catch-all for unmapped brownfields)
  5. EPA Superfund Redevelopment Mapper brownfields >100 acres, no reported
     redevelopment (curated ACRES subset, see EPA-540-S-26-001)
  6. EPA ACRES/SEMS/RCRA brownfields (corroboration registry, not standalone)

All sources are standardised to a common schema, deduplicated by proximity,
and saved as a single GeoDataFrame in EPSG:5070.

Output: data/processed/candidates_wv.gpkg
        data/processed/candidates_wv.csv
"""

import re
import sys
import pandas as pd
import geopandas as gpd
import numpy as np
from pathlib import Path
from shapely.geometry import Point
from shapely.strtree import STRtree

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT       = Path(__file__).parent
RAW        = ROOT / "data" / "raw"
EIA_DIR    = RAW / "eia860"
FRS_DIR    = RAW / "frs"
OSM_DIR    = RAW / "osm"
TENNESSEE    = ROOT / "data" / "westvirginia" / "raw"
BF_DIR     = TENNESSEE / "brownfields"
PROC_DIR   = ROOT / "data" / "processed"
PROC_DIR.mkdir(exist_ok=True)

TARGET_STATES = ["WV"]
STATE         = "_".join(sorted(TARGET_STATES)).lower()
MIN_MW        = 50
TARGET_CRS    = "EPSG:5070"

DEDUP_RADIUS_M = 500

FRS_NAICS_PREFIXES = (
    "321",  # Wood product / lumber mills
    "322",  # Paper/pulp mills
    "324",  # Petroleum refining / pipelines
    "325",  # Chemical mfg
    "327",  # Nonmetallic minerals / cement
    "331",  # Primary metals / steel
)

NAICS_LABEL = {
    "321": "Wood/Lumber Mill",
    "322": "Paper/Pulp Mill",
    "324": "Petroleum Refinery",
    "325": "Chemical Plant",
    "327": "Minerals/Cement Plant",
    "331": "Steel/Metals Plant",
}

FUEL_TYPE_MAP = {
    "NG": "Gas", "DFO": "Oil", "RFO": "Oil",
    "BIT": "Coal", "SUB": "Coal", "LIG": "Coal",
    "NUC": "Nuclear", "PC": "Coal", "RC": "Coal",
    "WC": "Coal", "OBG": "Biomass",
}

SCHEMA = [
    "source", "site_id", "Plant_Name", "State", "County",
    "Street_Address", "City",
    "Latitude", "Longitude",
    "brownfield_type", "total_mw",
    "retirement_year", "technology",
    "Grid_Voltage_kV",
    "Natural_Gas_Pipeline_Name_1",
    "Name_of_Water_Source",
    "naics_code", "has_air_permit", "geometry",
]


def clean_col(name: str) -> str:
    return name.replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")


# ---------------------------------------------------------------------------
# Source 1: EIA 860 Retired Generators
# ---------------------------------------------------------------------------
print("=" * 60)
print("Source 1 — EIA 860 Retired Power Plants (WV)")
print("=" * 60)

gen = pd.read_excel(
    EIA_DIR / "3_1_Generator_Y2023.xlsx",
    sheet_name="Retired and Canceled", header=1
)
print(f"  Raw retired/canceled generators: {len(gen):,}")
gen = gen[gen["Status"] == "RE"].copy()
gen = gen[gen["State"].isin(TARGET_STATES)].copy()
gen["Nameplate Capacity (MW)"] = pd.to_numeric(gen["Nameplate Capacity (MW)"], errors="coerce")
gen = gen[gen["Nameplate Capacity (MW)"] >= MIN_MW].copy()
print(f"  After RE + state + ≥{MIN_MW}MW filter: {len(gen):,} generators")

gen_active = pd.read_excel(
    EIA_DIR / "3_1_Generator_Y2023.xlsx",
    sheet_name="Operable", header=1
)
active_plant_codes = set(gen_active["Plant Code"].dropna().astype(int))
n_before = len(gen)
gen["Plant Code"] = pd.to_numeric(gen["Plant Code"], errors="coerce")
gen = gen[~gen["Plant Code"].isin(active_plant_codes)].copy()
print(f"  After removing plants with active generators: {len(gen):,} "
      f"(dropped {n_before - len(gen):,} partially-retired plants)")

agg = (
    gen.groupby(["Plant Code", "Plant Name", "State", "County"])
    .agg(
        total_mw        = ("Nameplate Capacity (MW)", "sum"),
        primary_fuel    = ("Energy Source 1", lambda x: x.mode().iloc[0] if len(x) else "UNK"),
        retirement_year = ("Retirement Year", "max"),
        technology      = ("Technology", lambda x: x.mode().iloc[0] if len(x) else "Unknown"),
    )
    .reset_index()
)

plant = pd.read_excel(EIA_DIR / "2___Plant_Y2023.xlsx", sheet_name="Plant", header=1)
plant = plant[[
    "Plant Code", "Street Address", "City", "Latitude", "Longitude",
    "Grid Voltage (kV)", "Natural Gas Pipeline Name 1", "Name of Water Source",
]].copy()
plant["Plant Code"] = pd.to_numeric(plant["Plant Code"], errors="coerce")
agg = agg.merge(plant, on="Plant Code", how="left")
agg = agg.dropna(subset=["Latitude", "Longitude"])
agg["Latitude"]  = pd.to_numeric(agg["Latitude"],  errors="coerce")
agg["Longitude"] = pd.to_numeric(agg["Longitude"], errors="coerce")
agg = agg.dropna(subset=["Latitude", "Longitude"])
agg = agg[
    (agg["Latitude"].between(24, 50)) &
    (agg["Longitude"].between(-125, -66))
]

eia_gdf = gpd.GeoDataFrame(
    agg,
    geometry=[Point(r["Longitude"], r["Latitude"]) for _, r in agg.iterrows()],
    crs="EPSG:4326",
).to_crs(TARGET_CRS)

eia_gdf["source"]        = "EIA860"
eia_gdf["site_id"]       = "EIA_" + eia_gdf["Plant Code"].astype(str)
eia_gdf["brownfield_type"] = eia_gdf["primary_fuel"].map(FUEL_TYPE_MAP).fillna("Other")
eia_gdf["naics_code"]    = ""
eia_gdf["has_air_permit"] = False
eia_gdf = eia_gdf.rename(columns={
    "Plant Name":                  "Plant_Name",
    "Street Address":              "Street_Address",
    "Grid Voltage (kV)":           "Grid_Voltage_kV",
    "Natural Gas Pipeline Name 1": "Natural_Gas_Pipeline_Name_1",
    "Name of Water Source":        "Name_of_Water_Source",
})

print(f"  EIA 860 candidates: {len(eia_gdf):,} plants")


# ---------------------------------------------------------------------------
# Source 1b: Global Energy Monitor Coal Plant Tracker (retired/mothballed)
# ---------------------------------------------------------------------------
# Cross-checks EIA-860 for retired coal plants -- catches plants below
# EIA-860's 50MW reporting floor. GEM's site-level data is gated behind an
# email signup form (https://globalenergymonitor.org/projects/global-coal-plant-tracker/download-data/),
# not a public API, so this reads a manually-downloaded local file rather
# than fetching live. Verified live against the January 2026 release: 71
# unique retired/mothballed plants across our 9 states after aggregating
# GEM's unit-level rows by location, of which most duplicate an existing
# EIA-860 candidate (harmless -- EIA-860 wins the dedup below since it has
# priority and richer attributes) and 2 are genuinely below the 50MW floor.
print("\n" + "=" * 60)
print("Source 1b — GEM Global Coal Plant Tracker (retired/mothballed) (WV)")
print("=" * 60)

GEM_DIR = TENNESSEE / "gem"
gem_files = sorted(GEM_DIR.glob("Global-Coal-Plant-Tracker*.xlsx")) if GEM_DIR.exists() else []

gem_gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=TARGET_CRS))

if not gem_files:
    print(f"  [skip] No GEM Coal Plant Tracker file found in {GEM_DIR}")
    print(f"  Manual download (email signup required): "
          f"https://globalenergymonitor.org/projects/global-coal-plant-tracker/download-data/")
    print(f"  Place the downloaded .xlsx in {GEM_DIR}/ (any filename starting "
          f"with 'Global-Coal-Plant-Tracker' works, so future dated re-downloads don't need code changes)")
else:
    try:
        gem_raw = pd.read_excel(gem_files[-1], sheet_name="Units", header=0)
        sub = gem_raw[
            (gem_raw["Country/Area"] == "United States")
            & (gem_raw["Subnational unit (province, state)"] == "West Virginia")
            & (gem_raw["Status"].isin(["retired", "mothballed"]))
        ].copy()
        print(f"  {len(sub):,} retired/mothballed GEM coal units in West Virginia "
              f"(from {gem_files[-1].name})")

        if len(sub) > 0:
            plants = sub.groupby("GEM location ID", as_index=False).agg(
                Plant_Name=("Plant name", "first"),
                total_mw=("Capacity (MW)", "sum"),
                City=("Location", "first"),
                Latitude=("Latitude", "first"),
                Longitude=("Longitude", "first"),
                retirement_year=("Retired year", "max"),
                technology=("Combustion technology", "first"),
            )
            plants = plants.dropna(subset=["Latitude", "Longitude"])

            gem_gdf = gpd.GeoDataFrame({
                "source":         "GEM_COAL",
                "site_id":        "GEM_" + plants["GEM location ID"].astype(str),
                "Plant_Name":     plants["Plant_Name"],
                "State":          "WV",
                "County":         "",
                "Street_Address": "",
                "City":           plants["City"].fillna(""),
                "Latitude":       plants["Latitude"],
                "Longitude":      plants["Longitude"],
                "brownfield_type": "Coal",
                "total_mw":       plants["total_mw"],
                "retirement_year": plants["retirement_year"],
                "technology":     plants["technology"].fillna("Unknown"),
                "Grid_Voltage_kV": np.nan,
                "Natural_Gas_Pipeline_Name_1": "",
                "Name_of_Water_Source": "",
                "naics_code":     "",
                "has_air_permit": False,
                "geometry":       gpd.points_from_xy(plants["Longitude"], plants["Latitude"]),
            }, crs="EPSG:4326").to_crs(TARGET_CRS)

            print(f"  GEM coal candidates (aggregated to plant level): {len(gem_gdf):,}")
    except Exception as _e_gem:
        print(f"  [warn] GEM Coal Plant Tracker parse failed: {_e_gem}")


# ---------------------------------------------------------------------------
# Confirmed Brownfield Registry
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("Building Confirmed Brownfield Registry (WV)")
print("=" * 60)

def load_confirmed_ids(path: Path, label: str) -> set:
    if not path.exists():
        print(f"  [skip] {label} — file not found, run download_westvirginia.py")
        return set()
    gj = gpd.read_file(path)
    gj.columns = [c.upper() for c in gj.columns]
    id_col = next((c for c in gj.columns if "REGISTRY_ID" in c), None)
    if not id_col:
        print(f"  [warn] {label} — no REGISTRY_ID column")
        return set()
    ids = set(gj[id_col].dropna().astype(str).str.strip())
    print(f"  {label}: {len(ids):,} confirmed registry IDs")
    return ids

strong_ids  = load_confirmed_ids(BF_DIR / "epa_acres_al.geojson",         "EPA ACRES")
strong_ids |= load_confirmed_ids(BF_DIR / "epa_sems_al.geojson",          "EPA SEMS")

rcra_only_ids = load_confirmed_ids(BF_DIR / "epa_rcra_inactive_al.geojson", "RCRA Inactive")
rcra_only_ids -= strong_ids

confirmed_ids = strong_ids | rcra_only_ids
print(f"  Strong (ACRES+SEMS): {len(strong_ids):,} | Weak (RCRA-only): {len(rcra_only_ids):,}")


# ---------------------------------------------------------------------------
# Source 2: EPA FRS Manufacturing Facilities
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("Source 2 — EPA FRS Manufacturing Facilities (WV)")
print("=" * 60)

frs_frames = []
for state in TARGET_STATES:
    fac_path   = FRS_DIR / f"{state}_FACILITY_FILE.CSV"
    naics_path = FRS_DIR / f"{state}_NAICS_FILE.CSV"

    if not fac_path.exists() or not naics_path.exists():
        print(f"  [skip] {state} FRS files not found — run download_data_al.py first")
        continue

    fac = pd.read_csv(fac_path, low_memory=False, encoding="latin-1")
    naics = pd.read_csv(naics_path, low_memory=False, encoding="latin-1")

    fac.columns   = [c.strip().upper() for c in fac.columns]
    naics.columns = [c.strip().upper() for c in naics.columns]

    id_col = "REGISTRY_ID" if "REGISTRY_ID" in fac.columns else fac.columns[0]
    naics_id_col = "REGISTRY_ID" if "REGISTRY_ID" in naics.columns else naics.columns[0]
    naics_code_col = next((c for c in naics.columns if "NAICS" in c and "CODE" in c), None)

    if naics_code_col is None:
        print(f"  [warn] {state}: could not find NAICS code column in {naics_path.name}")
        continue

    naics[naics_code_col] = naics[naics_code_col].astype(str).str.strip()
    target_mask = naics[naics_code_col].str[:3].isin(FRS_NAICS_PREFIXES)
    naics_target = naics[target_mask][[naics_id_col, naics_code_col]].drop_duplicates(naics_id_col)

    merged = fac.merge(naics_target, left_on=id_col, right_on=naics_id_col, how="inner")
    print(f"  {state}: {len(fac):,} facilities → {len(merged):,} in target NAICS")

    if "PGM_SYS_ACRNMS" in merged.columns:
        merged["_has_air_permit"] = merged["PGM_SYS_ACRNMS"].str.contains(
            "AIRS/AFS|ICIS-AIR", na=False
        )
        n_air = merged["_has_air_permit"].sum()
        print(f"    {n_air} sites with air permit history")

        name_col = next((c for c in merged.columns if "PRIMARY_NAME" in c or "NAME" in c), None)

        # Exclude active GHG reporters
        ghg_direct = merged["PGM_SYS_ACRNMS"].str.contains("E-GGRT", na=False)

        COMMON_WORDS = {
            "THE", "CITY", "STATE", "TENNESSEE", "PLANT", "FACILITY", "STATION",
            "PORT", "NORTH", "SOUTH", "EAST", "WEST", "INTERNATIONAL", "INC",
            "CORP", "LLC", "COMPANY", "STEEL", "CHEMICAL",
            "NASHVILLE", "MEMPHIS", "KNOXVILLE", "CHATTANOOGA", "CLARKSVILLE",
            "INDUSTRIES", "SERVICES", "PRODUCTS", "MANUFACTURING", "SYSTEMS",
            "ENERGY", "POWER", "GAS", "OIL", "PETROLEUM", "REFINERY",
            "TERMINAL", "SERVICE", "INDUSTRIAL", "GROUP",
            "RESOURCES", "MATERIALS", "SUPPLY", "DIVISION", "OPERATIONS",
            "WORKS", "NATIONAL", "AMERICAN", "GENERAL", "UNITED", "FEDERAL",
            "CORP", "CORPORATION", "LIMITED", "PARTNERS", "HOLDINGS",
        }
        ghg_reporters = fac[fac["PGM_SYS_ACRNMS"].str.contains("E-GGRT", na=False)]
        ghg_company_roots = set()
        for gname in ghg_reporters[name_col].dropna().str.upper():
            words = [w for w in gname.split() if w not in COMMON_WORDS and len(w) >= 4]
            if len(words) >= 2:
                ghg_company_roots.add(f"{words[0]} {words[1]}")
            elif len(words) == 1 and len(words[0]) >= 6:
                ghg_company_roots.add(words[0])

        def _has_ghg_parent(site_name: str) -> bool:
            if not site_name or pd.isna(site_name):
                return False
            name_upper = str(site_name).upper()
            return any(root in name_upper for root in ghg_company_roots)

        ghg_parent = merged[name_col].apply(_has_ghg_parent)
        ghg_active = ghg_direct | ghg_parent
        n_ghg_direct = ghg_direct.sum()
        n_ghg_parent = (ghg_parent & ~ghg_direct).sum()
        merged = merged[~ghg_active]
        print(f"    → {len(merged):,} after removing {n_ghg_direct + n_ghg_parent} "
              f"active GHG reporters ({n_ghg_direct} direct + {n_ghg_parent} parent-company match)")

        has_scale = (
            merged["PGM_SYS_ACRNMS"].str.contains("TRIS",     na=False)
            | merged["PGM_SYS_ACRNMS"].str.contains("RCRAINFO", na=False)
            | merged["_has_air_permit"]
        )
        n_no_scale = (~has_scale).sum()
        merged = merged[has_scale]
        print(f"    → {len(merged):,} with industrial scale signal "
              f"(dropped {n_no_scale:,} lacking TRI/RCRA/air-permit history)")

    if "SITE_TYPE_NAME" in merged.columns:
        portable = merged["SITE_TYPE_NAME"].str.upper().str.strip() == "PORTABLE"
        n_port = portable.sum()
        merged = merged[~portable]
        print(f"    → {len(merged):,} after removing {n_port} portable sites")

    ACTIVE_FRS_EXCLUDE: set[str] = set()
    if id_col in merged.columns:
        active_mask = merged[id_col].astype(str).isin(ACTIVE_FRS_EXCLUDE)
        n_manual = active_mask.sum()
        merged = merged[~active_mask]
        if n_manual:
            print(f"    → {len(merged):,} after removing {n_manual} confirmed-active sites")

    if name_col := next((c for c in merged.columns if "PRIMARY_NAME" in c or "NAME" in c), None):
        ACTIVE_NAME_PATTERNS = [
            r"ready.?mix", r"redi.?mix", r"sand\s+and\s+gravel", r"sand\s*&\s*gravel",
            r"concrete\s+supply", r"ready\s+mix\s+concrete",
            r"\basphalt\b", r"\bpaving\b", r"\bbituminous\b", r"\btarmac\b",
            r"\bpallet\b", r"\bpallets\b",
            r"\bconstruction\s+co(mpany)?\b", r"\bcontracting\b", r"\bcontractors?\b",
            r"\bbuilders?\b",
            r"\btreatment\s*plant\b", r"\bwastewater\b", r"\bwater\s*treatment\b",
        ]
        name_active = merged[name_col].str.lower().str.contains(
            "|".join(ACTIVE_NAME_PATTERNS), na=False, regex=True
        )
        n_name = name_active.sum()
        merged = merged[~name_active]
        if n_name:
            print(f"    → {len(merged):,} after removing {n_name} active-pattern names")

    ACTIVE_COMPANY_PATTERNS = [
        r"\bquikrete\b", r"\bcemex\b", r"\bholcim\b", r"\blafarge\b",
        r"\bcrh\b", r"\boldcastle\b", r"\beagle\s*materials\b",
        r"\bmartin\s*marietta\b", r"\bvulcan\s*materials\b",
        r"\bthermo\s*fisher\b", r"\bkaiser\s*aluminum\b",
        r"\bclayton\s*homes\b", r"\bberkshire\b",
        r"\bprysmian\b", r"\bgeneral\s*cable\b",
        r"\bmcwane\b", r"\btyler\s*pipe\b",
        r"\bnucor\b", r"\bssab\b", r"\boutokumpu\b",
        r"\bconcrete\s*batch\b", r"\bbatch\s*plant\b",
        r"\bhot\s*mix\b", r"\bconcrete\s*plant\b",
    ]
    if name_col:
        company_active = merged[name_col].str.lower().str.contains(
            "|".join(ACTIVE_COMPANY_PATTERNS), na=False, regex=True
        )
        n_company = company_active.sum()
        merged = merged[~company_active]
        if n_company:
            print(f"    → {len(merged):,} after removing {n_company} active-company-pattern names")

    if naics_code_col in merged.columns:
        is_327 = merged[naics_code_col].astype(str).str[:3] == "327"
        if name_col:
            concrete_keywords = merged[name_col].str.lower().str.contains(
                r"concrete|batch|lime\s*plant|aggregate|crushed|quarry|gravel|"
                r"precast|redi.?mix|pavestone|pavers?\b",
                na=False, regex=True
            )
            drop_327 = is_327 & concrete_keywords
            n_327 = drop_327.sum()
            merged = merged[~drop_327]
            if n_327:
                print(f"    → {len(merged):,} after removing {n_327} NAICS 327 concrete/batch operations")

    fed_col = next((c for c in merged.columns if "FEDERAL_FACILITY" in c), None)
    if fed_col:
        is_federal = merged[fed_col].notna() & (merged[fed_col].str.strip() != "")
        n_fed = is_federal.sum()
        merged = merged[~is_federal]
        if n_fed:
            print(f"    → {len(merged):,} after removing {n_fed} federal/DOE facilities")

    lat_col = next((c for c in merged.columns if "LAT" in c), None)
    lon_col = next((c for c in merged.columns if "LON" in c or "LONG" in c), None)
    name_col = next((c for c in merged.columns if "PRIMARY_NAME" in c or "FAC_NAME" in c or "NAME" in c), None)
    addr_col = next((c for c in merged.columns if "LOCATION_ADDRESS" in c or "ADDRESS" in c), None)
    city_col = next((c for c in merged.columns if "CITY" in c), None)
    county_col = next((c for c in merged.columns if "COUNTY" in c), None)

    if not lat_col or not lon_col:
        print(f"  [warn] {state}: no lat/lon columns found")
        continue

    merged[lat_col] = pd.to_numeric(merged[lat_col], errors="coerce")
    merged[lon_col] = pd.to_numeric(merged[lon_col], errors="coerce")
    merged = merged.dropna(subset=[lat_col, lon_col])
    merged = merged[
        merged[lat_col].between(24, 50) &
        merged[lon_col].between(-125, -66)
    ]

    frame = pd.DataFrame({
        "source":         "FRS",
        "site_id":        "FRS_" + merged[id_col].astype(str),
        "Plant_Name":     merged[name_col].fillna("Unknown") if name_col else "Unknown",
        "State":          state,
        "County":         merged[county_col].fillna("") if county_col else "",
        "Street_Address": merged[addr_col].fillna("") if addr_col else "",
        "City":           merged[city_col].fillna("") if city_col else "",
        "Latitude":       merged[lat_col],
        "Longitude":      merged[lon_col],
        "naics_code":     merged[naics_code_col].astype(str).str[:3],
        "brownfield_type": merged[naics_code_col].astype(str).str[:3].map(NAICS_LABEL).fillna("Industrial"),
        "total_mw":       np.nan,
        "retirement_year": np.nan,
        "technology":     "Industrial",
        "Grid_Voltage_kV": np.nan,
        "Natural_Gas_Pipeline_Name_1": "",
        "Name_of_Water_Source": "",
        "has_air_permit": merged["_has_air_permit"].astype(bool)
                          if "_has_air_permit" in merged.columns else False,
    })

    # Corroboration filter
    if confirmed_ids:
        raw_ids = merged[id_col].astype(str).str.strip()
        pathway_a = raw_ids.isin(strong_ids)
        n_strong = pathway_a.sum()

        pathway_b = pd.Series(False, index=merged.index)
        needs_signals = merged[~pathway_a.values].copy()

        if len(needs_signals) > 0:
            from difflib import get_close_matches as _gcm
            signals_count = pd.Series(0, index=needs_signals.index)

            name_col_ns = next(
                (c for c in needs_signals.columns if "PRIMARY_NAME" in c), None
            )
            pgm_col = next(
                (c for c in needs_signals.columns if "PGM_SYS_ACRNMS" in c), None
            )

            for idx in needs_signals.index:
                pgm = str(needs_signals.at[idx, pgm_col]).upper() if pgm_col else ""
                n_signals = 0

                if "TRIS" in pgm:
                    n_signals += 1
                if any(p in pgm for p in ["AIRS/AFS", "ICIS-AIR", "EIS", "CAMD"]):
                    n_signals += 1
                if "NPDES" in pgm:
                    n_signals += 1

                signals_count.at[idx] = n_signals

            pathway_b_mask = signals_count >= 2
            n_rescued = pathway_b_mask.sum()

            for orig_idx, passes in zip(needs_signals.index, pathway_b_mask):
                if passes:
                    pathway_b.at[orig_idx] = True

            if n_rescued > 0:
                rescued_names = merged.loc[
                    pathway_b & ~pathway_a, name_col
                ].tolist() if name_col else []
                print(f"    Pathway B rescued {n_rescued} sites with 2+ retirement signals:")
                for rn in rescued_names[:10]:
                    print(f"      + {rn}")

        combined = pathway_a | pathway_b
        n_before = len(frame)
        frame = frame[combined.values]
        n_dropped = n_before - len(frame)
        print(f"    → {len(frame):,} after corroboration filter "
              f"({n_strong} ACRES/SEMS + {(combined & ~pathway_a).sum()} pathway-B; "
              f"dropped {n_dropped:,})")
    else:
        print("    [warn] No confirmed registry loaded — skipping corroboration")

    frs_frames.append(frame)

if frs_frames:
    frs_df = pd.concat(frs_frames, ignore_index=True)
    frs_gdf = gpd.GeoDataFrame(
        frs_df,
        geometry=[Point(r["Longitude"], r["Latitude"]) for _, r in frs_df.iterrows()],
        crs="EPSG:4326",
    ).to_crs(TARGET_CRS)
    print(f"\n  Total FRS candidates: {len(frs_gdf):,}")
    for bt, grp in frs_gdf.groupby("brownfield_type"):
        print(f"    {bt}: {len(grp):,}")
else:
    frs_gdf = gpd.GeoDataFrame(columns=SCHEMA, geometry=[], crs=TARGET_CRS)
    print("  No FRS data loaded.")


# ---------------------------------------------------------------------------
# Source 3: EPA TRI (Toxics Release Inventory) — closed industrial reporters
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("Source 3 — EPA TRI Closed Facilities (WV)")
print("=" * 60)

TRI_CACHE = TENNESSEE / "tri_wv_facilities.csv"
TRI_NAICS_PREFIXES = FRS_NAICS_PREFIXES  # same target sectors

tri_gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=TARGET_CRS))

try:
    if not TRI_CACHE.exists():
        TRI_CACHE.parent.mkdir(parents=True, exist_ok=True)
        import requests as _req_tri
        import xml.etree.ElementTree as _ET
        # EPA Envirofacts TRI_FACILITY — returns XML, 2000 records per page
        rows, offset = [], 0
        print("  Downloading TRI facilities from EPA Envirofacts …")
        while True:
            url = (
                f"https://data.epa.gov/efservice/TRI_FACILITY"
                f"/STATE_ABBR/WV/{offset}:{offset+1999}"
            )
            resp = _req_tri.get(url, timeout=30)
            if not resp.ok:
                break
            root = _ET.fromstring(resp.content)
            batch = [{child.tag: child.text for child in elem} for elem in root]
            if not batch:
                break
            rows.extend(batch)
            print(f"    {len(rows):,} fetched …", end="\r")
            if len(batch) < 2000:
                break
            offset += 2000
        print()
        tri_raw = pd.DataFrame(rows)
        tri_raw.to_csv(TRI_CACHE, index=False)
        print(f"  Downloaded {len(tri_raw):,} WV TRI facilities → {TRI_CACHE.name}")
    else:
        tri_raw = pd.read_csv(TRI_CACHE, low_memory=False)
        print(f"  Loaded {len(tri_raw):,} WV TRI facilities from cache")

    # TRI_FACILITY columns: FACILITY_NAME, FAC_CLOSED_IND, TRI_FACILITY_ID,
    # EPA_REGISTRY_ID (= FRS REGISTRY_ID), FAC_LATITUDE/FAC_LONGITUDE (DDMMSS),
    # PREF_LATITUDE/PREF_LONGITUDE (decimal, partial), COUNTY_NAME, CITY_NAME

    # Filter to officially closed facilities only
    tri_closed = tri_raw[tri_raw["FAC_CLOSED_IND"].astype(str).isin(["1", "1.0"])].copy()
    print(f"  Officially closed (FAC_CLOSED_IND=1): {len(tri_closed):,}")

    # Resolve coordinates: prefer PREF_LATITUDE (decimal) → convert FAC_LATITUDE (DDMMSS)
    def _ddmmss_to_dd(v):
        """Convert integer DDMMSS (e.g. 360511 → 36.0864) to decimal degrees."""
        try:
            v = int(float(v))
            d, ms = divmod(abs(v), 10000)
            m, s = divmod(ms, 100)
            return d + m / 60 + s / 3600
        except Exception:
            return np.nan

    tri_closed["_lat"] = pd.to_numeric(tri_closed["PREF_LATITUDE"], errors="coerce")
    tri_closed["_lon"] = pd.to_numeric(tri_closed["PREF_LONGITUDE"], errors="coerce")
    fac_lat_mask = tri_closed["_lat"].isna()
    tri_closed.loc[fac_lat_mask, "_lat"] = tri_closed.loc[fac_lat_mask, "FAC_LATITUDE"].apply(_ddmmss_to_dd)
    tri_closed.loc[fac_lat_mask, "_lon"] = -(tri_closed.loc[fac_lat_mask, "FAC_LONGITUDE"].apply(_ddmmss_to_dd))

    tri_closed = tri_closed[tri_closed["_lat"].between(34, 37) & tri_closed["_lon"].between(-91, -81)]
    print(f"  With valid WV coordinates: {len(tri_closed):,}")

    # Join NAICS from FRS NAICS file via EPA_REGISTRY_ID
    frs_naics_path = FRS_DIR / "TN_NAICS_FILE.CSV"
    if frs_naics_path.exists():
        frs_naics = pd.read_csv(frs_naics_path, low_memory=False)
        naics_map = (
            frs_naics.dropna(subset=["NAICS_CODE", "REGISTRY_ID"])
            .astype({"REGISTRY_ID": str, "NAICS_CODE": str})
            .groupby("REGISTRY_ID")["NAICS_CODE"]
            .first().to_dict()
        )
        tri_closed["_reg_id"] = tri_closed["EPA_REGISTRY_ID"].astype(str).str.replace(r"\.0$", "", regex=True)
        tri_closed["_naics"] = tri_closed["_reg_id"].map(naics_map).fillna("")
    else:
        tri_closed["_naics"] = ""

    target_naics3 = {p[:3] for p in TRI_NAICS_PREFIXES}
    tri_sector = tri_closed[tri_closed["_naics"].str[:3].isin(target_naics3)].copy()
    print(f"  In target NAICS sectors: {len(tri_sector):,}")

    def _tri_brownfield(naics_str):
        for prefix, label in NAICS_LABEL.items():
            if str(naics_str).startswith(prefix[:3]):
                return label
        return "Industrial"

    if len(tri_sector) > 0:
        tri_pts = gpd.GeoDataFrame(
            tri_sector,
            geometry=gpd.points_from_xy(tri_sector["_lon"], tri_sector["_lat"]),
            crs="EPSG:4326",
        ).to_crs(TARGET_CRS)

        tri_gdf = gpd.GeoDataFrame({
            "source":       "TRI",
            "site_id":      "TRI_" + tri_sector["TRI_FACILITY_ID"].astype(str).values,
            "Plant_Name":   tri_sector["FACILITY_NAME"].values,
            "State":        tri_sector.get("STATE_ABBR", pd.Series("WV", index=tri_sector.index)).values,
            "County":       tri_sector.get("COUNTY_NAME", pd.Series("", index=tri_sector.index)).values,
            "Street_Address": tri_sector.get("STREET_ADDRESS", pd.Series("", index=tri_sector.index)).values,
            "City":         tri_sector.get("CITY_NAME", pd.Series("", index=tri_sector.index)).values,
            "Latitude":     tri_sector["_lat"].values,
            "Longitude":    tri_sector["_lon"].values,
            "brownfield_type": [_tri_brownfield(n) for n in tri_sector["_naics"].values],
            "total_mw":     np.nan,
            "retirement_year": np.nan,
            "technology":   "",
            "Grid_Voltage_kV": np.nan,
            "Natural_Gas_Pipeline_Name_1": np.nan,
            "Name_of_Water_Source": "",
            "naics_code":   tri_sector["_naics"].values,
            "has_air_permit": False,
            "geometry":     tri_pts.geometry.values,
        }, crs=TARGET_CRS)
        print(f"  TRI candidates: {len(tri_gdf):,} sites")

except Exception as _e_tri:
    print(f"  [warn] TRI source failed: {_e_tri}")


# ---------------------------------------------------------------------------
# Source 4: OSM Industrial Landuse
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("Source 4 — OSM Industrial Landuse Polygons (WV)")
print("=" * 60)

osm_path = OSM_DIR / "industrial_landuse_wv.geojson"
if not osm_path.exists():
    osm_path = OSM_DIR / "industrial_landuse.geojson"

if osm_path.exists():
    osm_raw = gpd.read_file(osm_path).to_crs(TARGET_CRS)
    print(f"  Loaded {len(osm_raw):,} OSM industrial features")

    osm_named = osm_raw[osm_raw["name"].notna() & (osm_raw["name"].str.strip() != "")].copy()
    print(f"  Named sites only: {len(osm_named):,}")

    has_disused  = osm_named.get("disused", pd.Series("", index=osm_named.index)).str.strip().str.lower() == "yes"
    has_end_date = osm_named.get("end_date", pd.Series("", index=osm_named.index)).str.strip() != ""
    osm_closed = osm_named[has_disused | has_end_date].copy()
    print(f"  With closure signal (disused=yes or end_date set): {len(osm_closed):,}")
    osm_named = osm_closed

    def osm_brownfield_type(row):
        name = str(row.get("name", "")).lower()
        industrial = str(row.get("industrial", "")).lower()
        for keyword, label in [
            ("paper", "Paper/Pulp Mill"), ("pulp", "Paper/Pulp Mill"),
            ("mill", "Paper/Pulp Mill"), ("steel", "Steel/Metals Plant"),
            ("metal", "Steel/Metals Plant"), ("refin", "Petroleum Refinery"),
            ("chemi", "Chemical Plant"), ("cement", "Minerals/Cement Plant"),
            ("textile", "Textile Mill"), ("manufactur", "Industrial"),
        ]:
            if keyword in name or keyword in industrial:
                return label
        return "Industrial"

    osm_named["brownfield_type"] = osm_named.apply(osm_brownfield_type, axis=1)

    osm_gdf = gpd.GeoDataFrame({
        "source":         "OSM",
        "site_id":        "OSM_" + osm_named["osm_id"].astype(str),
        "Plant_Name":     osm_named["name"],
        "State":          "",
        "County":         "",
        "Street_Address": "",
        "City":           "",
        "Latitude":       osm_named.geometry.y if osm_named.crs.to_epsg() == 4326
                          else osm_named.to_crs("EPSG:4326").geometry.y,
        "Longitude":      osm_named.geometry.x if osm_named.crs.to_epsg() == 4326
                          else osm_named.to_crs("EPSG:4326").geometry.x,
        "naics_code":     "",
        "brownfield_type": osm_named["brownfield_type"],
        "total_mw":       np.nan,
        "retirement_year": np.nan,
        "technology":     "Industrial",
        "Grid_Voltage_kV": np.nan,
        "Natural_Gas_Pipeline_Name_1": "",
        "Name_of_Water_Source": "",
        "has_air_permit": False,
        "geometry":       osm_named.geometry,
    }, crs=TARGET_CRS)

    # Spatial join to get state
    states_path = ROOT / "data" / "raw" / "census" / "tl_2023_us_state.zip"
    if not states_path.exists():
        import requests, io, zipfile
        print("  Downloading Census state boundaries for OSM state assignment …")
        r = requests.get(
            "https://www2.census.gov/geo/tiger/TIGER2023/STATE/tl_2023_us_state.zip",
            timeout=60,
        )
        states_path.write_bytes(r.content)
        with zipfile.ZipFile(states_path) as z:
            z.extractall(states_path.parent)

    states_shp = next(states_path.parent.glob("tl_2023_us_state.shp"), None)
    if states_shp:
        states = gpd.read_file(states_shp)[["STUSPS", "geometry"]].to_crs(TARGET_CRS)
        states = states[states["STUSPS"].isin(TARGET_STATES)]
        joined = gpd.sjoin(
            osm_gdf[["site_id", "geometry"]],
            states,
            how="left", predicate="within"
        )
        osm_gdf["State"] = joined["STUSPS"].values
        osm_gdf = osm_gdf[osm_gdf["State"].isin(TARGET_STATES)].copy()
        print(f"  OSM sites in AL: {len(osm_gdf):,}")
    else:
        osm_gdf = osm_gdf.iloc[0:0]

    if len(osm_gdf) > 0:
        import time, requests as _req
        print(f"  Reverse-geocoding {len(osm_gdf)} OSM sites via Nominatim …")
        wgs84 = osm_gdf.to_crs("EPSG:4326")
        headers = {"User-Agent": "DataCenterScreener/1.0 arthur.b.fok@gmail.com"}
        for idx, row in wgs84.iterrows():
            lat, lon = row.geometry.y, row.geometry.x
            try:
                r = _req.get(
                    "https://nominatim.openstreetmap.org/reverse",
                    params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
                    headers=headers, timeout=10,
                )
                data = r.json()
                addr = data.get("address", {})
                road    = addr.get("road", addr.get("pedestrian", ""))
                number  = addr.get("house_number", "")
                street  = f"{number} {road}".strip() if number else road
                city    = addr.get("city", addr.get("town", addr.get("village", "")))
                county  = addr.get("county", "").replace(" County", "").upper()
                osm_gdf.at[idx, "Street_Address"] = street
                osm_gdf.at[idx, "City"]           = city
                if county and not osm_gdf.at[idx, "County"]:
                    osm_gdf.at[idx, "County"] = county
            except Exception:
                pass
            time.sleep(1.1)
        print("  Reverse-geocoding complete.")

    print(f"  By type:")
    for bt, grp in osm_gdf.groupby("brownfield_type"):
        print(f"    {bt}: {len(grp):,}")
else:
    osm_gdf = gpd.GeoDataFrame(columns=SCHEMA, geometry=[], crs=TARGET_CRS)
    print("  OSM industrial file not found — run download_data_al.py first")


print("\n  (ACRES/SEMS/RCRA used for FRS corroboration only — not standalone sources)")


# ---------------------------------------------------------------------------
# Source 5: EPA Superfund Redevelopment Mapper — Brownfields >100 acres
# ---------------------------------------------------------------------------
# Curated ACRES subset EPA built for exactly this reuse case (see EPA-540-S-26-001,
# "Guidance on the Redevelopment of Superfund and Brownfield Sites as AI Data
# Centers", Jan 2026): brownfield properties >100 acres with no reported
# redevelopment. Comes with pre-computed proximity flags (electric line, rail,
# highway, water) that double as a free sanity check against filter_pipeline.py's
# own spatial filters.
print("\n" + "=" * 60)
print("Source 5 — EPA Redevelopment Mapper Brownfields >100ac (WV)")
print("=" * 60)

SRP_CACHE = TENNESSEE / "srp_redev_mapper_wv.csv"
SRP_URL = (
    "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/"
    "Brownfield_Properties_Over_100_Acres_view/FeatureServer/0/query"
)

redev_gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=TARGET_CRS))

try:
    if not SRP_CACHE.exists():
        SRP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        import requests as _req_srp
        print("  Downloading EPA Redevelopment Mapper brownfields …")
        all_feats = []
        for state in TARGET_STATES:
            resp = _req_srp.get(SRP_URL, params={
                "where": f"State='{state}'",
                "outFields": "*",
                "f": "json",
            }, timeout=30)
            resp.raise_for_status()
            feats = resp.json().get("features", [])
            all_feats.extend(f["attributes"] for f in feats)
        srp_raw = pd.DataFrame(all_feats)
        srp_raw.to_csv(SRP_CACHE, index=False)
        print(f"  Downloaded {len(srp_raw):,} sites (>100 acres) → {SRP_CACHE.name}")
    else:
        srp_raw = pd.read_csv(SRP_CACHE, low_memory=False)
        print(f"  Loaded {len(srp_raw):,} sites from cache")

    if len(srp_raw) > 0:
        srp_raw = srp_raw.dropna(subset=["Latitude", "Longitude"])

        # Exclude sites already redeveloped or marked ready-for-use — we want
        # undeveloped brownfield land, not a site a competitor already built on.
        already_ready = srp_raw["Ready_for_Anticipated_Use_"].astype(str).str.strip() == "Yes"
        has_redev_date = srp_raw["Redevelopment_Start_Date"].notna()
        n_before = len(srp_raw)
        srp_raw = srp_raw[~(already_ready | has_redev_date)].copy()
        print(f"  {n_before:,} → {len(srp_raw):,} after excluding sites already "
              f"redeveloped or ready-for-use")

        # Exclude federally/state protected parkland — legally undevelopable
        # regardless of infrastructure merit. EPA's ACRES data includes some
        # large tracts with legacy industrial history that are now protected
        # (e.g. Palo Alto Battlefield National Historical Park in TX, former
        # ranchland now NPS-managed) — not real redevelopment candidates.
        PROTECTED_LAND_PATTERNS = [
            r"national\s+historical?\s+park", r"national\s+park",
            r"state\s+park", r"wildlife\s+refuge", r"national\s+monument",
            r"national\s+forest", r"national\s+recreation\s+area",
        ]
        is_protected = srp_raw["Property_Name"].astype(str).str.lower().str.contains(
            "|".join(PROTECTED_LAND_PATTERNS), regex=True, na=False
        )
        n_protected = is_protected.sum()
        srp_raw = srp_raw[~is_protected].copy()
        if n_protected:
            print(f"  Excluded {n_protected} protected park/monument site(s) "
                  f"(not legally developable regardless of infrastructure)")

        def _srp_brownfield_type(row):
            text = f"{row.get('Property_Name','')} {row.get('Property_Highlights','')}".lower()
            for keyword, label in [
                ("paper", "Paper/Pulp Mill"), ("pulp", "Paper/Pulp Mill"),
                ("steel", "Steel/Metals Plant"), ("smelt", "Steel/Metals Plant"),
                ("foundry", "Steel/Metals Plant"), ("mill", "Steel/Metals Plant"),
                ("refin", "Petroleum Refinery"), ("chemi", "Chemical Plant"),
                ("cement", "Minerals/Cement Plant"),
            ]:
                if keyword in text:
                    return label
            return "Industrial" if str(row.get("Industrial", "")).strip() == "Yes" else "Other Brownfield"

        srp_raw["brownfield_type"] = srp_raw.apply(_srp_brownfield_type, axis=1)

        redev_gdf = gpd.GeoDataFrame({
            "source":         "SRP_REDEV_MAPPER",
            "site_id":        "SRP_" + srp_raw["Property_ID"].astype(str),
            "Plant_Name":     srp_raw["Property_Name"],
            "State":          srp_raw["State"],
            "County":         "",
            "Street_Address": srp_raw["Address"].fillna(""),
            "City":           srp_raw["City"].fillna(""),
            "Latitude":       srp_raw["Latitude"],
            "Longitude":      srp_raw["Longitude"],
            "brownfield_type": srp_raw["brownfield_type"],
            "total_mw":       np.nan,
            "retirement_year": np.nan,
            "technology":     "",
            "Grid_Voltage_kV": np.nan,
            "Natural_Gas_Pipeline_Name_1": "",
            "Name_of_Water_Source": "",
            "naics_code":     "",
            "has_air_permit": False,
            "geometry":       gpd.points_from_xy(srp_raw["Longitude"], srp_raw["Latitude"]),
        }, crs="EPSG:4326").to_crs(TARGET_CRS)

        print(f"  Redevelopment Mapper candidates: {len(redev_gdf):,} sites "
              f"(all ≥100 acres by construction)")
        for bt, grp in redev_gdf.groupby("brownfield_type"):
            print(f"    {bt}: {len(grp):,}")
    else:
        print("  No sites returned for target state(s).")

except Exception as _e_srp:
    print(f"  [warn] Redevelopment Mapper source failed: {_e_srp}")


# ---------------------------------------------------------------------------
# Combine all sources
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("Combining all sources")
print("=" * 60)

all_frames = [df for df in [eia_gdf, gem_gdf, redev_gdf, frs_gdf, tri_gdf, osm_gdf] if len(df) > 0]

common_cols = [c for c in SCHEMA if c != "geometry"]
for i, df in enumerate(all_frames):
    for col in common_cols:
        if col not in df.columns:
            all_frames[i][col] = np.nan if col in ("total_mw", "retirement_year", "Grid_Voltage_kV") else ""

combined = gpd.GeoDataFrame(
    pd.concat([df[common_cols + ["geometry"]] for df in all_frames], ignore_index=True),
    crs=TARGET_CRS,
)
print(f"  Combined (pre-dedup): {len(combined):,}")
print(f"  By source: {combined['source'].value_counts().to_dict()}")


# ---------------------------------------------------------------------------
# Deduplicate by proximity (500m radius)
# ---------------------------------------------------------------------------
print(f"\nDeduplicating within {DEDUP_RADIUS_M}m radius …")
print("  Priority: EIA860 > GEM Coal Tracker > SRP Redevelopment Mapper > FRS > TRI > OSM")

combined["_source_order"] = combined["source"].map(
    {"EIA860": 0, "GEM_COAL": 1, "SRP_REDEV_MAPPER": 2, "FRS": 3, "TRI": 4, "OSM": 5}
).fillna(6)
combined = combined.sort_values("_source_order").reset_index(drop=True)

keep = []
tree = STRtree(combined.geometry.values)
dropped = set()
for i, geom in enumerate(combined.geometry.values):
    if i in dropped:
        continue
    keep.append(i)
    nearby = tree.query(geom, predicate="dwithin", distance=DEDUP_RADIUS_M)
    for j in nearby:
        if j != i and j not in dropped:
            dropped.add(j)

gdf = combined.iloc[keep].drop(columns="_source_order").reset_index(drop=True)
print(f"  After dedup: {len(gdf):,} unique sites (removed {len(combined) - len(gdf):,} duplicates)")


# ---------------------------------------------------------------------------
# Coarse pre-filter: within 25 miles of any 230kV+ transmission line
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("Coarse pre-filter: within 25 miles of 230kV+ transmission line")
print("=" * 60)

TX_PARQUET  = RAW / "hifld" / "transmission_lines.parquet"
TX_GEOJSON  = RAW / "hifld" / "transmission_lines.geojson"
COARSE_DIST = 25 * 1609.344

if TX_PARQUET.exists():
    tx_lines = gpd.read_parquet(TX_PARQUET, columns=["VOLTAGE", "STATUS", "geometry"])
elif TX_GEOJSON.exists():
    tx_lines = gpd.read_file(TX_GEOJSON)[["VOLTAGE", "STATUS", "geometry"]].to_crs(TARGET_CRS)
else:
    tx_lines = None
    print("  [WARN] Transmission line data not found — skipping coarse pre-filter")

if tx_lines is not None:
    tx_lines["VOLTAGE"] = pd.to_numeric(tx_lines["VOLTAGE"], errors="coerce")
    tx_hv = tx_lines[
        (tx_lines["VOLTAGE"] >= 230) & (tx_lines["STATUS"] == "IN SERVICE")
    ]
    print(f"  230kV+ in-service lines: {len(tx_hv):,}")

    minx, miny, maxx, maxy = gdf.total_bounds
    tx_region = tx_hv.cx[minx - COARSE_DIST : maxx + COARSE_DIST,
                          miny - COARSE_DIST : maxy + COARSE_DIST]

    tree = STRtree(tx_region.geometry.values)
    result = tree.query(gdf.geometry.values, predicate="dwithin", distance=COARSE_DIST)
    near_tx = set(result[0].tolist())
    mask = [i in near_tx for i in range(len(gdf))]

    n_before = len(gdf)
    gdf = gpd.GeoDataFrame(gdf[mask].reset_index(drop=True), crs=TARGET_CRS)
    print(f"  {n_before:,} → {len(gdf):,} candidates within 25 mi of 230kV+ line "
          f"(dropped {n_before - len(gdf):,})")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n--- Final Candidate Pool ---")
print(f"  Total: {len(gdf):,}")
print(f"\n  By source:")
for s, grp in gdf.groupby("source"):
    print(f"    {s}: {len(grp):,}")
print(f"\n  By state:")
for s, grp in gdf.groupby("State"):
    print(f"    {s}: {len(grp):,}")
print(f"\n  By brownfield type:")
for t, grp in gdf.groupby("brownfield_type"):
    print(f"    {t}: {len(grp):,}")


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
print("\nSaving …")
gdf.to_file(PROC_DIR / f"candidates_{STATE}.gpkg", driver="GPKG")
gdf.drop(columns="geometry").to_csv(PROC_DIR / f"candidates_{STATE}.csv", index=False)
print(f"  {PROC_DIR / f'candidates_{STATE}.gpkg'}")
print(f"  {PROC_DIR / f'candidates_{STATE}.csv'}")
print(f"\nDone. {len(gdf):,} candidates ready for filter_pipeline_al.py")
