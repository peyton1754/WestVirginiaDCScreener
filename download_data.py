"""
download_data.py
Downloads all free datasets needed for the West Virginia data center screening pipeline.

Reuses the same national datasets as the TX pipeline (HIFLD, Census, EPA)
but downloads WV-specific FRS and NHD data.

Sources:
  - EIA Form 860 (retired generators)           — shared with TX
  - HIFLD: Transmission Lines, Substations       — shared with TX
  - Census Urban Areas (2020)                    — shared with TX
  - EPA Nonattainment Areas                      — shared with TX
  - NHD (National Hydrography Dataset)           — WV-specific
  - EPA FRS Manufacturing                        — WV-specific
  - OSM Industrial Landuse                       — WV-specific bbox

Run: python3 download_data.py
"""

import os
import io
import sys
import json
import zipfile
import requests
from pathlib import Path
from tqdm import tqdm

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"

EIA_DIR    = RAW / "eia860"
HIFLD_DIR  = RAW / "hifld"
CENSUS_DIR = RAW / "census"
EPA_DIR    = RAW / "epa"
NHD_DIR    = RAW / "nhd"
FRS_DIR    = RAW / "frs"
OSM_DIR    = RAW / "osm"

STATES = ["WV"]

STATE_NAMES = {
    # NHD's S3 filenames use an underscore, not a space, between words
    "WV": "West_Virginia",
}

# ---------------------------------------------------------------------------
# Helpers (same as download_data.py)
# ---------------------------------------------------------------------------

def _is_valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            z.testzip()
        return True
    except Exception:
        return False


def download_file(url: str, dest: Path, desc: str = "", chunk_size: int = 1 << 20) -> Path:
    if dest.exists():
        if dest.suffix == ".zip" and not _is_valid_zip(dest):
            print(f"  [corrupt] Removing bad ZIP and re-downloading: {dest.name}")
            dest.unlink()
        else:
            print(f"  [skip] {dest.name} already exists")
            return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading {desc or dest.name} …")
    try:
        with requests.get(url, stream=True, timeout=120,
                          headers={"User-Agent": "DataCenterScreener/1.0"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            with open(dest, "wb") as f, tqdm(
                total=total, unit="B", unit_scale=True,
                desc=dest.name, leave=False
            ) as bar:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    f.write(chunk)
                    bar.update(len(chunk))
    except requests.HTTPError as e:
        print(f"  [ERROR] HTTP {e.response.status_code} for {url}")
        if dest.exists():
            dest.unlink()
        raise
    except Exception as e:
        print(f"  [ERROR] {e}")
        if dest.exists():
            dest.unlink()
        raise
    return dest


def unzip(zip_path: Path, out_dir: Path) -> None:
    marker = out_dir / f".unzipped_{zip_path.stem}"
    if marker.exists():
        print(f"  [skip] {zip_path.name} already unzipped")
        return
    print(f"  Unzipping {zip_path.name} …")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    marker.touch()


def hifld_rest_download(service_url: str, dest: Path, desc: str = "",
                        where: str = "1=1", out_sr: int = 4326,
                        max_record_count: int = 2000) -> Path:
    if dest.exists():
        print(f"  [skip] {dest.name} already exists")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    query_url = service_url.rstrip("/") + "/query"
    params_base = {
        "where": where,
        "outFields": "*",
        "f": "geojson",
        "outSR": out_sr,
        "returnGeometry": "true",
    }

    count_resp = requests.get(
        query_url,
        params={"where": where, "returnCountOnly": "true", "f": "json"},
        timeout=60,
    )
    count_resp.raise_for_status()
    total = count_resp.json().get("count", 0)
    print(f"  Fetching {desc or dest.stem}: {total:,} features …")

    features = []
    offset = 0
    with tqdm(total=total, unit="feat", leave=False) as bar:
        while True:
            params = {
                **params_base,
                "resultOffset": offset,
                "resultRecordCount": max_record_count,
            }
            resp = requests.get(query_url, params=params, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("features", [])
            if not batch:
                break
            features.extend(batch)
            bar.update(len(batch))
            offset += len(batch)
            if len(batch) < max_record_count:
                break

    geojson = {"type": "FeatureCollection", "features": features}
    with open(dest, "w") as f:
        json.dump(geojson, f)
    print(f"  Saved {len(features):,} features → {dest.name}")
    return dest


# ---------------------------------------------------------------------------
# 1. EIA Form 860 (shared — same download as TX)
# ---------------------------------------------------------------------------

def download_eia860():
    print("\n=== EIA Form 860 ===")
    url = "https://www.eia.gov/electricity/data/eia860/archive/xls/eia8602023.zip"
    dest = EIA_DIR / "eia8602023.zip"
    if dest.exists():
        try:
            with zipfile.ZipFile(dest) as _z:
                pass
        except Exception:
            print(f"  Removing corrupt prior download: {dest.name}")
            dest.unlink()
    download_file(url, dest, desc="EIA 860 2023 ZIP")
    unzip(dest, EIA_DIR)


# ---------------------------------------------------------------------------
# 2. HIFLD Transmission Lines (shared — same download as TX)
# ---------------------------------------------------------------------------

HIFLD_SERVICES = {
    "transmission_lines": (
        "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/"
        "Electric_Power_Transmission_Lines/FeatureServer/0",
        "Electric Power Transmission Lines",
    ),
}

def download_hifld():
    print("\n=== HIFLD Layers ===")
    for layer_name, (svc_url, desc) in HIFLD_SERVICES.items():
        dest = HIFLD_DIR / f"{layer_name}.geojson"
        try:
            hifld_rest_download(svc_url, dest, desc=desc)
        except Exception as e:
            print(f"  [WARN] Could not download {layer_name} via REST: {e}")


def download_hifld_substations():
    """
    Download WV substations from HIFLD (Electric Substations layer).
    Replaces the previous OSM Overpass source which had incomplete voltage
    tagging and lower coverage. HIFLD provides MAX_VOLT, MIN_VOLT, LINES
    (connected line count), and STATUS for all US substations.
    """
    print("\n=== HIFLD Substations (WV, ≥115kV in-service) ===")
    dest = HIFLD_DIR / "substations.geojson"
    if dest.exists():
        print(f"  [skip] substations.geojson already exists")
        return

    BASE = ("https://services6.arcgis.com/OO2s4OoyCZkYJ6oE/arcgis/rest/"
            "services/Substations/FeatureServer/0")
    features = []
    offset = 0
    batch = 2000
    while True:
        r = requests.get(f"{BASE}/query", params={
            "where": "STATE='WV'",
            "outFields": "NAME,CITY,STATE,COUNTY,LATITUDE,LONGITUDE,MAX_VOLT,MIN_VOLT,STATUS,TYPE,LINES",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": batch,
            "f": "geojson",
        }, headers={"User-Agent": "DataCenterScreener/1.0"}, timeout=60)
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            break
        features.extend(feats)
        if len(feats) < batch:
            break
        offset += len(feats)

    # Keep in-service substations ≥115kV; normalise voltage field for compatibility
    in_service = []
    for f in features:
        a = f["properties"]
        status = str(a.get("STATUS", "")).upper()
        max_v = float(a.get("MAX_VOLT", 0) or 0)
        if "IN SERVICE" in status and max_v >= 115:
            min_v = float(a.get("MIN_VOLT", max_v) or max_v)
            min_v = max_v if min_v < 0 else min_v
            f["properties"]["voltage"] = f"{int(max_v*1000)};{int(min_v*1000)}"
            in_service.append(f)

    import json as _json
    geojson = {"type": "FeatureCollection", "features": in_service}
    with open(dest, "w") as fh:
        _json.dump(geojson, fh)
    print(f"  WV substations fetched: {len(features):,} total, {len(in_service):,} in-service ≥115kV")
    print(f"  Saved: {dest.name}")


def download_osm_infrastructure():
    print("\n=== OSM Gas Pipelines ===")
    # Note: substations are now fetched from HIFLD (see download_hifld_substations).
    # This OSM gas-pipeline layer is superseded by download_gas_pipelines_wv.py's
    # EIA-sourced fetch (the one filter_pipeline.py actually reads) and is not
    # consumed downstream; kept only for reference/manual comparison.

    BBOX = "37.1,-82.7,40.7,-77.6"
    overpass_url = "https://overpass-api.de/api/interpreter"

    layers = {
        "gas_pipelines": (
            f"""
            [out:json][timeout:180];
            (
              way["man_made"="pipeline"]["substance"="natural_gas"]({BBOX});
              way["man_made"="pipeline"]["type"="gas"]({BBOX});
              way["pipeline"="gas"]({BBOX});
            );
            out geom;
            """,
            "OSM natural gas pipelines",
        ),
    }

    for layer_name, (query, desc) in layers.items():
        dest = HIFLD_DIR / f"{layer_name}.geojson"
        if dest.exists():
            print(f"  [skip] {layer_name}.geojson already exists")
            continue
        print(f"  Fetching {desc} from Overpass API …")
        try:
            resp = requests.post(
                overpass_url,
                data={"data": query},
                timeout=300,
                headers={"User-Agent": "DataCenterScreener/1.0"},
            )
            resp.raise_for_status()
            osm = resp.json()
            elements = osm.get("elements", [])

            features = []
            for el in elements:
                el_type = el.get("type")
                props = {**el.get("tags", {}), "osm_id": el.get("id"), "osm_type": el_type}

                if el_type == "node":
                    geom = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
                elif el_type in ("way", "relation") and "center" in el:
                    c = el["center"]
                    geom = {"type": "Point", "coordinates": [c["lon"], c["lat"]]}
                elif el_type == "way" and "geometry" in el:
                    coords = [[g["lon"], g["lat"]] for g in el["geometry"]]
                    geom = {"type": "LineString", "coordinates": coords}
                else:
                    continue

                features.append({"type": "Feature", "geometry": geom, "properties": props})

            geojson = {"type": "FeatureCollection", "features": features}
            with open(dest, "w") as f:
                json.dump(geojson, f)
            print(f"  Saved {len(features):,} features → {dest.name}")

        except Exception as e:
            if dest.exists():
                dest.unlink()
            print(f"  [ERROR] OSM download failed for {layer_name}: {e}")


# ---------------------------------------------------------------------------
# 3. Census Urban Areas (shared)
# ---------------------------------------------------------------------------

def download_census_urban_areas():
    print("\n=== Census Urban Areas ===")
    url = ("https://www2.census.gov/geo/tiger/TIGER2023/UAC/"
           "tl_2023_us_uac20.zip")
    dest = CENSUS_DIR / "tl_2023_us_uac20.zip"
    download_file(url, dest, desc="Census Urban Areas 2023")
    unzip(dest, CENSUS_DIR)


# ---------------------------------------------------------------------------
# 4. EPA Nonattainment Areas (shared)
# ---------------------------------------------------------------------------

def download_epa_nonattainment():
    print("\n=== EPA Nonattainment Areas ===")
    layers = {
        "8hour_ozone": (
            "https://www3.epa.gov/airquality/greenbook/shapefile/ozone_8hr_2015std_naa_shapefile.zip",
            "EPA 8-Hour Ozone 2015 Std Nonattainment",
        ),
        "pm25_annual": (
            "https://www3.epa.gov/airquality/greenbook/shapefile/pm25_2012std_naa_shapefile.zip",
            "EPA PM2.5 2012 Std Nonattainment",
        ),
    }
    for name, (url, desc) in layers.items():
        dest = EPA_DIR / f"{name}.zip"
        try:
            download_file(url, dest, desc=desc)
            unzip(dest, EPA_DIR / name)
        except Exception as e:
            print(f"  [WARN] Could not download EPA {name}: {e}")


# ---------------------------------------------------------------------------
# 5. NHD Water Bodies — West Virginia
# ---------------------------------------------------------------------------

def download_nhd():
    print("\n=== NHD Water Bodies (West Virginia) ===")
    base_url = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHD/State/Shape/"

    for state in STATES:
        state_name = STATE_NAMES[state]
        fname = f"NHD_H_{state_name}_State_Shape.zip"
        url = base_url + fname
        dest = NHD_DIR / fname
        try:
            download_file(url, dest, desc=f"NHD {state}")
            unzip(dest, NHD_DIR / state)
        except Exception as e:
            print(f"  [WARN] NHD download failed for {state}: {e}")


# ---------------------------------------------------------------------------
# 6. EPA FRS Manufacturing — West Virginia
# ---------------------------------------------------------------------------

FRS_BASE = "https://ordsext.epa.gov/FLA/www3/state_files/state_combined_{state}.zip"

def download_epa_frs():
    print("\n=== EPA FRS (Manufacturing Brownfields — WV) ===")
    FRS_DIR.mkdir(parents=True, exist_ok=True)

    for state_abbr in STATES:
        fac_dest   = FRS_DIR / f"{state_abbr}_FACILITY_FILE.CSV"
        naics_dest = FRS_DIR / f"{state_abbr}_NAICS_FILE.CSV"

        if fac_dest.exists() and naics_dest.exists():
            print(f"  [skip] {state_abbr} FRS files already exist")
            continue

        url = FRS_BASE.format(state=state_abbr.lower())
        print(f"  Downloading FRS {state_abbr} (~50 MB) …")
        try:
            r = requests.get(url, stream=True, timeout=300,
                             headers={"User-Agent": "DataCenterScreener/1.0"})
            r.raise_for_status()
            zip_bytes = io.BytesIO(r.content)

            with zipfile.ZipFile(zip_bytes) as zf:
                names = zf.namelist()
                for name in names:
                    base = name.split("/")[-1].upper()
                    if base == f"{state_abbr}_FACILITY_FILE.CSV":
                        fac_dest.write_bytes(zf.read(name))
                        print(f"    Extracted {name} → {fac_dest.name}")
                    elif base == f"{state_abbr}_NAICS_FILE.CSV":
                        naics_dest.write_bytes(zf.read(name))
                        print(f"    Extracted {name} → {naics_dest.name}")
        except Exception as e:
            print(f"  [WARN] FRS download failed for {state_abbr}: {e}")


# ---------------------------------------------------------------------------
# 7. OSM Industrial Landuse — West Virginia bbox
# ---------------------------------------------------------------------------

def download_osm_industrial():
    print("\n=== OSM Industrial Landuse Polygons (WV) ===")
    OSM_DIR.mkdir(parents=True, exist_ok=True)

    dest = OSM_DIR / "industrial_landuse_wv.geojson"
    if dest.exists():
        print(f"  [skip] {dest.name} already exists")
        return

    BBOX = "37.1,-82.7,40.7,-77.6"

    query = f"""
    [out:json][timeout:300];
    (
      way["landuse"="industrial"]({BBOX});
      relation["landuse"="industrial"]({BBOX});
      way["man_made"="works"]({BBOX});
      way["industrial"~"paper|pulp|steel|metal|refinery|chemical|mill|factory"]({BBOX});
    );
    out center tags;
    """

    print("  Querying Overpass API for WV industrial landuse …")
    try:
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=360,
            headers={"User-Agent": "DataCenterScreener/1.0"},
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
        print(f"  Received {len(elements):,} OSM elements")

        features = []
        for el in elements:
            center = el.get("center")
            if not center:
                continue
            tags = el.get("tags", {})
            props = {
                "osm_id":     el.get("id"),
                "osm_type":   el.get("type"),
                "name":       tags.get("name", ""),
                "landuse":    tags.get("landuse", ""),
                "industrial": tags.get("industrial", ""),
                "man_made":   tags.get("man_made", ""),
                "operator":   tags.get("operator", ""),
                "start_date": tags.get("start_date", ""),
                "end_date":   tags.get("end_date", ""),
                "disused":    tags.get("disused", ""),
            }
            geom = {"type": "Point", "coordinates": [center["lon"], center["lat"]]}
            features.append({"type": "Feature", "geometry": geom, "properties": props})

        geojson = {"type": "FeatureCollection", "features": features}
        with open(dest, "w") as f:
            json.dump(geojson, f)
        print(f"  Saved {len(features):,} features → {dest.name}")

    except Exception as e:
        print(f"  [ERROR] OSM industrial query failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("Data Center Screener — West Virginia Dataset Download")
    print("=" * 60)

    steps = [
        ("EIA 860", download_eia860),
        ("HIFLD Transmission Lines", download_hifld),
        ("HIFLD Substations (WV)", download_hifld_substations),
        ("OSM Gas Pipelines", download_osm_infrastructure),
        ("Census Urban Areas", download_census_urban_areas),
        ("EPA Nonattainment", download_epa_nonattainment),
        ("NHD Water Bodies", download_nhd),
        ("EPA FRS Manufacturing", download_epa_frs),
        ("OSM Industrial Landuse", download_osm_industrial),
    ]

    failed = []
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            print(f"\n[ERROR] {name} step failed: {e}")
            failed.append(name)

    print("\n" + "=" * 60)
    if failed:
        print(f"Completed with errors in: {', '.join(failed)}")
        print("Check warnings above and download missing files manually.")
        sys.exit(1)
    else:
        print("All West Virginia datasets downloaded successfully.")
        print(f"Raw data in: {RAW}")
