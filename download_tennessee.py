"""
download_tennessee.py
Downloads Tennessee-specific datasets for detailed data center site screening.

Datasets:
  1. EIA Form 861 utility rates       — avg industrial $/kWh by utility in AL
  2. HIFLD utility service territories — which utility covers each area (clip to AL)
  3. Census Opportunity Zones          — HUD OZ designation list + Census tracts
  4. Census county boundaries (AL)     — TIGER 2023 for spatial joins
  5. FCC broadband Form 477            — fiber presence at census block level
  6. USGS stream gauges (AL)           — active gauge locations for water access scoring
  7. EPA ACRES brownfields             — confirmed brownfield sites with assessment status
  8. EPA SEMS Superfund sites          — Superfund site assessments
  9. BLS LAUS county employment        — labor market size by county

Run: python3 download_tennessee.py
"""

import io
import json
import time
import zipfile
import requests
import pandas as pd
from pathlib import Path
from tqdm import tqdm

ROOT = Path(__file__).parent
AL   = ROOT / "data" / "tennessee" / "raw"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path, desc: str = "",
                  headers: dict = None, chunk_size: int = 1 << 20) -> bool:
    if dest.exists():
        print(f"  [skip] {dest.name} already exists")
        return True
    print(f"  Downloading {desc or dest.name} …")
    try:
        r = requests.get(url, stream=True, timeout=120, headers=headers or {},
                         allow_redirects=True)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f, tqdm(total=total or None, unit="B",
                                          unit_scale=True, desc=f"  {desc[:45]}",
                                          leave=False) as bar:
            for chunk in r.iter_content(chunk_size):
                f.write(chunk)
                bar.update(len(chunk))
        print(f"    → {dest.name}  ({dest.stat().st_size / 1e6:.1f} MB)")
        return True
    except Exception as e:
        print(f"  [WARN] {desc}: {e}")
        if dest.exists():
            dest.unlink()
        return False


def unzip(src: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(dest_dir)
    print(f"    Extracted → {dest_dir.name}/")


def arcgis_query(service_url: str, dest: Path, desc: str,
                 where: str = "1=1", out_fields: str = "*",
                 page_size: int = 2000) -> bool:
    if dest.exists():
        print(f"  [skip] {dest.name} already exists")
        return True
    print(f"  Querying {desc} …")
    query_url = service_url.rstrip("/") + "/query"
    features, offset = [], 0
    while True:
        try:
            r = requests.get(query_url, params={
                "where": where, "outFields": out_fields,
                "resultOffset": offset, "resultRecordCount": page_size,
                "outSR": "4326", "f": "geojson",
            }, timeout=60)
            r.raise_for_status()
            batch = r.json().get("features", [])
        except Exception as e:
            print(f"  [WARN] {desc} page {offset}: {e}")
            break
        if not batch:
            break
        features.extend(batch)
        offset += len(batch)
        if len(batch) < page_size:
            break
    if not features:
        print(f"  [WARN] No features returned for {desc}")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    print(f"    → {dest.name}  ({len(features):,} features)")
    return True


# ---------------------------------------------------------------------------
# 1. EIA Form 861 — Utility Rates (AL)
# ---------------------------------------------------------------------------
print("\n=== 1. EIA Form 861 — Utility Rates (AL) ===")

eia861_zip = AL / "utility_territories" / "f8612022.zip"
if not eia861_zip.exists():
    download_file(
        url="https://www.eia.gov/electricity/data/eia861/archive/zip/f8612022.zip",
        dest=eia861_zip,
        desc="EIA Form 861 2022",
    )

if eia861_zip.exists():
    eia861_dir = AL / "utility_territories" / "eia861_2022"
    if not eia861_dir.exists():
        unzip(eia861_zip, eia861_dir)

    # Exclude Sales_Ult_Cust_CS_*.xlsx (Community Solar subset — a few hundred
    # rows nationwide) which glob() can match ahead of the real full sales file.
    sales_candidates = sorted(eia861_dir.glob("Sales_Ult_Cust*.xlsx"), key=lambda p: len(p.name))
    sales_xlsx = next((p for p in sales_candidates if "_CS_" not in p.name), None)
    if sales_xlsx:
        out = AL / "utility_territories" / "eia861_al_sales.csv"
        if not out.exists():
            df = pd.read_excel(sales_xlsx, header=2, dtype=str)
            df.columns = [str(c).strip() for c in df.columns]
            state_col = next((c for c in df.columns if "STATE" in c.upper()), None)
            if state_col:
                al_rows = df[df[state_col].astype(str).str.upper() == "TN"]
                out.parent.mkdir(parents=True, exist_ok=True)
                al_rows.to_csv(out, index=False)
                print(f"    AL utility rows: {len(al_rows)} → eia861_al_sales.csv")

# ---------------------------------------------------------------------------
# 2. EIA/HIFLD Utility Service Territories — clip to AL
# ---------------------------------------------------------------------------
print("\n=== 2. Utility Service Territories (AL) ===")

ut_dest = AL / "utility_territories" / "al_utility_territories.geojson"
if not ut_dest.exists():
    eia_st_zip = AL / "utility_territories" / "eia_service_areas_2022.zip"
    if not eia_st_zip.exists():
        download_file(
            url="https://www.eia.gov/maps/map_data/ElectricRetail_Territories.zip",
            dest=eia_st_zip,
            desc="EIA Utility Service Territories",
        )

    if eia_st_zip.exists():
        import geopandas as gpd
        sa_dir = AL / "utility_territories" / "eia_service_areas"
        if not sa_dir.exists():
            unzip(eia_st_zip, sa_dir)
        shp_files = list(sa_dir.rglob("*.shp"))
        if shp_files:
            gdf = gpd.read_file(shp_files[0])
            state_col = next(
                (c for c in gdf.columns if c.upper() in ("STATE", "ST", "STATE_ABBR")),
                None,
            )
            try:
                if state_col:
                    al_gdf = gdf[gdf[state_col].str.upper().str.strip() == "TN"]
                else:
                    al_bounds = (-88.5, 30.1, -84.9, 35.0)
                    al_gdf = gdf.cx[al_bounds[0]:al_bounds[2], al_bounds[1]:al_bounds[3]]
                ut_dest.parent.mkdir(parents=True, exist_ok=True)
                al_gdf.to_file(ut_dest, driver="GeoJSON")
                print(f"    AL utility territories: {len(al_gdf)} → al_utility_territories.geojson")
            except Exception as e:
                print(f"  [WARN] Clip to AL failed: {e}")


# ---------------------------------------------------------------------------
# 3. Census Opportunity Zones (HUD designation list)
# ---------------------------------------------------------------------------
print("\n=== 3. Opportunity Zones ===")

oz_csv = AL / "opportunity_zones" / "oz_designations.csv"
if not oz_csv.exists():
    try:
        from io import BytesIO
        r = requests.get(
            "https://www.cdfifund.gov/system/files/documents/designated-qozs.12.14.18.xlsx",
            timeout=60,
        )
        r.raise_for_status()
        df = pd.read_excel(BytesIO(r.content), header=4)
        df.columns = ["State", "County", "geoid", "Tract_Type", "ACS_Source"]
        df["geoid"] = df["geoid"].astype(str).str.replace(".", "", regex=False).str.strip()
        # AL Census tract GEOIDs start with "01"
        al_oz = df[df["geoid"].str.startswith("47")]
        oz_csv.parent.mkdir(parents=True, exist_ok=True)
        al_oz.to_csv(oz_csv, index=False)
        print(f"    → oz_designations.csv  ({len(al_oz)} AL opportunity zones)")
    except Exception as e:
        print(f"  [WARN] OZ download: {e}")

# Census tracts for Tennessee (FIPS 01)
al_tracts_zip = AL / "opportunity_zones" / "tl_2023_47_tract.zip"
download_file(
    url="https://www2.census.gov/geo/tiger/TIGER2023/TRACT/tl_2023_47_tract.zip",
    dest=al_tracts_zip,
    desc="AL census tracts 2023",
)
if al_tracts_zip.exists():
    tracts_dir = AL / "opportunity_zones" / "tracts"
    if not tracts_dir.exists():
        unzip(al_tracts_zip, tracts_dir)


# ---------------------------------------------------------------------------
# 4. Tennessee County Boundaries (Census TIGER)
# ---------------------------------------------------------------------------
print("\n=== 4. Tennessee County Boundaries ===")
import geopandas as gpd

counties_zip = AL / "census" / "tl_2023_us_county.zip"
download_file(
    url="https://www2.census.gov/geo/tiger/TIGER2023/COUNTY/tl_2023_us_county.zip",
    dest=counties_zip,
    desc="US County Boundaries 2023",
)
if counties_zip.exists():
    counties_dir = AL / "census" / "counties"
    if not counties_dir.exists():
        unzip(counties_zip, counties_dir)
        counties_shp = next(counties_dir.glob("*.shp"), None)
        if counties_shp:
            print("  Extracting and clipping to AL …")
            gdf = gpd.read_file(counties_shp)
            al_counties = gdf[gdf["STATEFP"] == "47"]
            al_counties.to_file(counties_dir / "al_counties.shp")
            print(f"    AL counties: {len(al_counties)}")


# ---------------------------------------------------------------------------
# 5. FCC Broadband Form 477 — AL fiber census blocks
# ---------------------------------------------------------------------------
print("\n=== 5. FCC Broadband (AL Fiber) ===")

fcc_dest = AL / "broadband" / "fcc_477_al_fiber.csv"
if not fcc_dest.exists():
    print("  Querying FCC Open Data for AL fiber providers …")
    try:
        rows = []
        offset = 0
        limit = 50000
        while True:
            r = requests.get(
                "https://opendata.fcc.gov/resource/jdr4-3q4p.json",
                params={
                    "$limit": limit,
                    "$offset": offset,
                    "$where": "stateabbr = 'TN' AND techcode = 50",
                    "$select": "blockcode,stateabbr,techcode,maxaddown,maxadup",
                },
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
            fcc_dest.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).rename(columns={
                "blockcode": "Census Block FIPS Code",
                "techcode": "Technology Code",
            }).to_csv(fcc_dest, index=False)
            print(f"    → fcc_477_al_fiber.csv  ({len(rows):,} records)")
    except Exception as e:
        print(f"  [WARN] FCC fiber query: {e}")

# Census blocks for AL (FIPS 01)
al_blocks_zip = AL / "broadband" / "tl_2020_47_tabblock20.zip"
download_file(
    url="https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/tl_2020_47_tabblock20.zip",
    dest=al_blocks_zip,
    desc="AL census blocks 2020 (FCC join geometry)",
)
if al_blocks_zip.exists():
    blocks_dir = AL / "broadband" / "blocks"
    if not blocks_dir.exists():
        unzip(al_blocks_zip, blocks_dir)


# ---------------------------------------------------------------------------
# 6. USGS Stream Gauges — active gauges in Tennessee
# ---------------------------------------------------------------------------
print("\n=== 6. USGS Stream Gauges (AL) ===")

usgs_dest = AL / "water" / "usgs_gauges_al.csv"
if not usgs_dest.exists():
    print("  Querying USGS NWIS for AL active stream gauges …")
    try:
        r = requests.get(
            "https://waterservices.usgs.gov/nwis/site/",
            params={
                "format":       "rdb",
                "stateCd":      "TN",
                "siteType":     "ST",
                "siteStatus":   "active",
                "hasDataTypeCd": "iv,dv",
                "outputDataTypeCd": "dv",
            },
            timeout=60,
        )
        r.raise_for_status()
        lines = [l for l in r.text.splitlines()
                 if not l.startswith("#") and l.strip()]
        if len(lines) > 2:
            from io import StringIO
            df = pd.read_csv(
                StringIO("\n".join(lines)),
                sep="\t", low_memory=False,
                skiprows=[1],
            )
            df = df[df["dec_lat_va"].notna() & df["dec_long_va"].notna()]
            usgs_dest.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(usgs_dest, index=False)
            print(f"    → usgs_gauges_al.csv  ({len(df):,} active gauges)")
    except Exception as e:
        print(f"  [WARN] USGS gauges: {e}")


# ---------------------------------------------------------------------------
# 7. EPA Confirmed Brownfield Sources (AL)
# ---------------------------------------------------------------------------
print("\n=== 7. EPA Confirmed Brownfield Sources (AL) ===")

FIELDS = (
    "REGISTRY_ID,PRIMARY_NAME,LOCATION_ADDRESS,CITY_NAME,STATE_CODE,"
    "COUNTY_NAME,LATITUDE83,LONGITUDE83,INTEREST_TYPE,ACTIVE_STATUS")

# 7a. EPA ACRES
acres_dest = AL / "brownfields" / "epa_acres_al.geojson"
arcgis_query(
    service_url="https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/0",
    dest=acres_dest,
    desc="EPA ACRES AL brownfields (layer 0)",
    where="STATE_CODE='TN'",
    out_fields=FIELDS,
)

# 7b. EPA SEMS
sems_dest = AL / "brownfields" / "epa_sems_al.geojson"
arcgis_query(
    service_url="https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21",
    dest=sems_dest,
    desc="EPA SEMS AL Superfund sites (layer 21)",
    where="STATE_CODE='TN'",
    out_fields=FIELDS,
)

# 7c. RCRA Inactive
rcra_dest = AL / "brownfields" / "epa_rcra_inactive_al.geojson"
arcgis_query(
    service_url="https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/17",
    dest=rcra_dest,
    desc="EPA RCRA Inactive AL handlers (layer 17)",
    where="STATE_CODE='TN'",
    out_fields=FIELDS,
)


# ---------------------------------------------------------------------------
# 8. ADEM Brownfield Database (Tennessee Dept of Environmental Management)
# ---------------------------------------------------------------------------
print("\n=== 8. ADEM Brownfield Data ===")
print("  ADEM does not publish a public brownfield GIS layer.")
print("  Using EPA ACRES + SEMS + RCRA for Tennessee brownfield corroboration.")
print("  For additional sites, check: https://adem.tennessee.gov/programs/land/brownfields.cnt")


# ---------------------------------------------------------------------------
# 9. BLS LAUS — County employment in Tennessee (via public API)
# ---------------------------------------------------------------------------
print("\n=== 9. BLS County Labor Market (AL, 2023) ===")
laus_dest = AL / "labor" / "bls_laus_al_2023.csv"
if not laus_dest.exists():
    print("  Fetching BLS LAUS county unemployment for AL …")
    try:
        # TN county FIPS: 47001–47189 (odd numbers, 95 counties)
        tn_fips = [f"{n:03d}" for n in range(1, 190, 2)]
        series = [f"LAUCN47{f}0000000006" for f in tn_fips]
        rows = []
        for i in range(0, len(series), 25):
            batch = series[i:i+25]
            r = requests.post(
                "https://api.bls.gov/publicAPI/v2/timeseries/data/",
                json={"seriesid": batch, "startyear": "2023", "endyear": "2023"},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            data = r.json()
            if data.get("status") == "REQUEST_SUCCEEDED":
                for s in data["Results"]["series"]:
                    sid = s["seriesID"]
                    fips = sid[6:9]
                    obs_list = s.get("data", [])
                    # Prefer the M13 annual average if present, else fall back
                    # to the most recent monthly observation (data[0] — BLS
                    # returns series in reverse-chronological order).
                    obs = next((o for o in obs_list if o.get("period") == "M13"), None)
                    obs = obs or (obs_list[0] if obs_list else None)
                    if obs:
                        rows.append({
                            "county_fips": f"47{fips}",
                            "year": obs["year"],
                            "employed": obs["value"],
                        })
            time.sleep(0.5)

        if rows:
            laus_dest.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_csv(laus_dest, index=False)
            print(f"    → bls_laus_al_2023.csv  ({len(rows)} county records)")
        else:
            print("  [WARN] No BLS data returned")
    except Exception as e:
        print(f"  [WARN] BLS LAUS: {e}")
else:
    print(f"  [skip] bls_laus_al_2023.csv already exists")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("Tennessee dataset download summary")
print("=" * 60)
total_size = 0
for folder in sorted(AL.iterdir()):
    if not folder.is_dir():
        continue
    files = [f for f in folder.rglob("*") if f.is_file()]
    size = sum(f.stat().st_size for f in files)
    total_size += size
    print(f"  {folder.name:<25s}  {len(files):3d} files   {size/1e6:7.1f} MB")
print(f"  {'TOTAL':<25s}  {'':>3s}         {total_size/1e6:7.1f} MB")
print("\nNext step: python3 build_candidates_al.py")
