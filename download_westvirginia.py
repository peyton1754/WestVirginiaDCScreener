"""
download_westvirginia.py
Downloads West Virginia-specific datasets for detailed data center site screening.

Datasets:
  1. EIA Form 861 utility rates       — avg industrial $/kWh by utility in WV
  2. HIFLD utility service territories — which utility covers each area (clip to WV)
  3. Census Opportunity Zones          — HUD OZ designation list + Census tracts
  4. Census county boundaries (WV)     — TIGER 2023 for spatial joins
  5. FCC broadband Form 477            — fiber presence at census block level
  6. USGS stream gauges (WV)           — active gauge locations for water access scoring
  7. EPA ACRES brownfields             — confirmed brownfield sites with assessment status
  8. EPA SEMS Superfund sites          — Superfund site assessments
  9. BLS LAUS county employment        — labor market size by county

Run: python3 download_westvirginia.py
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
AL   = ROOT / "data" / "westvirginia" / "raw"


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
# 1. EIA Form 861 — Utility Rates (WV)
# ---------------------------------------------------------------------------
print("\n=== 1. EIA Form 861 — Utility Rates (WV) ===")

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
                al_rows = df[df[state_col].astype(str).str.upper() == "WV"]
                out.parent.mkdir(parents=True, exist_ok=True)
                al_rows.to_csv(out, index=False)
                print(f"    WV utility rows: {len(al_rows)} → eia861_al_sales.csv")

# ---------------------------------------------------------------------------
# 2. EIA/HIFLD Utility Service Territories — clip to WV
# ---------------------------------------------------------------------------
print("\n=== 2. Utility Service Territories (WV) ===")

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
                    al_gdf = gdf[gdf[state_col].str.upper().str.strip() == "WV"]
                else:
                    # WV statewide bounding box (fallback if no state column)
                    al_bounds = (-82.7, 37.15, -77.65, 40.65)
                    al_gdf = gdf.cx[al_bounds[0]:al_bounds[2], al_bounds[1]:al_bounds[3]]
                ut_dest.parent.mkdir(parents=True, exist_ok=True)
                al_gdf.to_file(ut_dest, driver="GeoJSON")
                print(f"    WV utility territories: {len(al_gdf)} → al_utility_territories.geojson")
            except Exception as e:
                print(f"  [WARN] Clip to WV failed: {e}")


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
        # WV Census tract GEOIDs start with state FIPS "54"
        al_oz = df[df["geoid"].str.startswith("54")]
        oz_csv.parent.mkdir(parents=True, exist_ok=True)
        al_oz.to_csv(oz_csv, index=False)
        print(f"    → oz_designations.csv  ({len(al_oz)} WV opportunity zones)")
    except Exception as e:
        print(f"  [WARN] OZ download: {e}")

# Census tracts for West Virginia (FIPS 54)
al_tracts_zip = AL / "opportunity_zones" / "tl_2023_54_tract.zip"
download_file(
    url="https://www2.census.gov/geo/tiger/TIGER2023/TRACT/tl_2023_54_tract.zip",
    dest=al_tracts_zip,
    desc="WV census tracts 2023",
)
if al_tracts_zip.exists():
    tracts_dir = AL / "opportunity_zones" / "tracts"
    if not tracts_dir.exists():
        unzip(al_tracts_zip, tracts_dir)


# ---------------------------------------------------------------------------
# 4. West Virginia County Boundaries (Census TIGER)
# ---------------------------------------------------------------------------
print("\n=== 4. West Virginia County Boundaries ===")
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
            print("  Extracting and clipping to WV …")
            gdf = gpd.read_file(counties_shp)
            al_counties = gdf[gdf["STATEFP"] == "54"]
            al_counties.to_file(counties_dir / "al_counties.shp")
            print(f"    WV counties: {len(al_counties)}")


# ---------------------------------------------------------------------------
# 5. FCC Broadband Form 477 — WV fiber census blocks
# ---------------------------------------------------------------------------
print("\n=== 5. FCC Broadband (WV Fiber) ===")

fcc_dest = AL / "broadband" / "fcc_477_al_fiber.csv"
if not fcc_dest.exists():
    print("  Querying FCC Open Data for WV fiber providers …")
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
                    "$where": "stateabbr = 'WV' AND techcode = 50",
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

# Census blocks for WV (FIPS 54)
al_blocks_zip = AL / "broadband" / "tl_2020_54_tabblock20.zip"
download_file(
    url="https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/tl_2020_54_tabblock20.zip",
    dest=al_blocks_zip,
    desc="WV census blocks 2020 (FCC join geometry)",
)
if al_blocks_zip.exists():
    blocks_dir = AL / "broadband" / "blocks"
    if not blocks_dir.exists():
        unzip(al_blocks_zip, blocks_dir)


# ---------------------------------------------------------------------------
# 6. USGS Stream Gauges — active gauges in West Virginia
# ---------------------------------------------------------------------------
print("\n=== 6. USGS Stream Gauges (WV) ===")

usgs_dest = AL / "water" / "usgs_gauges_al.csv"
if not usgs_dest.exists():
    print("  Querying USGS NWIS for WV active stream gauges …")
    try:
        r = requests.get(
            "https://waterservices.usgs.gov/nwis/site/",
            params={
                "format":       "rdb",
                "stateCd":      "WV",
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
# 7. EPA Confirmed Brownfield Sources (WV)
# ---------------------------------------------------------------------------
print("\n=== 7. EPA Confirmed Brownfield Sources (WV) ===")

FIELDS = (
    "REGISTRY_ID,PRIMARY_NAME,LOCATION_ADDRESS,CITY_NAME,STATE_CODE,"
    "COUNTY_NAME,LATITUDE83,LONGITUDE83,INTEREST_TYPE,ACTIVE_STATUS")

# 7a. EPA ACRES
acres_dest = AL / "brownfields" / "epa_acres_al.geojson"
arcgis_query(
    service_url="https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/0",
    dest=acres_dest,
    desc="EPA ACRES WV brownfields (layer 0)",
    where="STATE_CODE='WV'",
    out_fields=FIELDS,
)

# 7b. EPA SEMS
sems_dest = AL / "brownfields" / "epa_sems_al.geojson"
arcgis_query(
    service_url="https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21",
    dest=sems_dest,
    desc="EPA SEMS WV Superfund sites (layer 21)",
    where="STATE_CODE='WV'",
    out_fields=FIELDS,
)

# 7c. RCRA Inactive
rcra_dest = AL / "brownfields" / "epa_rcra_inactive_al.geojson"
arcgis_query(
    service_url="https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/17",
    dest=rcra_dest,
    desc="EPA RCRA Inactive WV handlers (layer 17)",
    where="STATE_CODE='WV'",
    out_fields=FIELDS,
)


# ---------------------------------------------------------------------------
# 8. WVDEP Brownfield Data (WV Dept of Environmental Protection)
# ---------------------------------------------------------------------------
print("\n=== 8. WVDEP Brownfield Data ===")
print("  WVDEP's Voluntary Remediation Program (VRP) does not publish a public")
print("  brownfield GIS layer or bulk-downloadable site list.")
print("  Using EPA ACRES + SEMS + RCRA for WV brownfield corroboration.")
print("  For additional sites, check: https://dep.wv.gov/dlr/oer/brownfieldsection/")


# ---------------------------------------------------------------------------
# 9. BLS LAUS — County employment in West Virginia (via public API)
# ---------------------------------------------------------------------------
print("\n=== 9. BLS County Labor Market (WV, 2023) ===")
laus_dest = AL / "labor" / "bls_laus_al_2023.csv"
if not laus_dest.exists():
    print("  Fetching BLS LAUS county unemployment for WV …")
    try:
        # WV county FIPS: 54001–54109 (odd numbers, 55 counties)
        tn_fips = [f"{n:03d}" for n in range(1, 110, 2)]
        series = [f"LAUCN54{f}0000000006" for f in tn_fips]
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
                            "county_fips": f"54{fips}",
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
print("West Virginia dataset download summary")
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
print("\nNext step: python3 build_candidates.py")
