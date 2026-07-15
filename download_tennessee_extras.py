"""
download_tennessee_extras.py
Downloads the additional hazard / environment / connectivity / state datasets
for the Tennessee data center screening pipeline that are NOT already fetched by
download_data_al.py (national core) or download_tennessee.py (EIA/FCC/EPA brownfields).

Programmatic (downloaded here):
  - USFWS NWI wetlands geodatabase (AL)
  - USGS PAD-US 4.1 protected areas geodatabase (AL)
  - NOAA Storm Events (recent years, filtered to AL)
  - FEMA National Risk Index — county table (AL)
  - Workforce Tennessee WARN notices (CSV feed)

Per-site web APIs (already wired into filter_pipeline_al.py / score_and_export_al.py,
nothing to pre-download):
  - FEMA NFHL flood zones    → filter_pipeline_al.py (live ArcGIS query)
  - USGS NSHM seismic ASCE7  → score_and_export_al.py (live query)
  - USDA SSURGO soils        → score_and_export_al.py (live query)

Manual / no free statewide bulk endpoint (printed as reminders):
  - Tennessee statewide parcel data  (county-level / paid only)
  - Tennessee SOS business entities  (search-only system, no clean bulk download)
  - ADECA OWR water certificates   (eWater portal only, no bulk export)

Run: python3 download_tennessee_extras.py
"""

import io
import re
import gzip
import json
import zipfile
import requests
import pandas as pd
from pathlib import Path
from tqdm import tqdm

ROOT = Path(__file__).parent
AL   = ROOT / "data" / "tennessee" / "raw"
AL.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "DataCenterScreener/1.0 arthur.b.fok@gmail.com"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path, desc: str = "",
                  verify: bool = True, chunk_size: int = 1 << 20) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [skip] {dest.name} already exists ({dest.stat().st_size/1e6:.1f} MB)")
        return True
    print(f"  Downloading {desc or dest.name} …")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=300, headers=HEADERS,
                          verify=verify, allow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            with open(dest, "wb") as f, tqdm(total=total or None, unit="B",
                                             unit_scale=True, desc=f"  {desc[:40]}",
                                             leave=False) as bar:
                for chunk in r.iter_content(chunk_size):
                    f.write(chunk)
                    bar.update(len(chunk))
        print(f"    → {dest.name}  ({dest.stat().st_size/1e6:.1f} MB)")
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


# ---------------------------------------------------------------------------
# 1. USFWS NWI Wetlands — Tennessee geodatabase
# ---------------------------------------------------------------------------
def download_nwi():
    print("\n=== 1. USFWS NWI Wetlands (AL) ===")
    dest_zip = AL / "nwi" / "AL_geodatabase_wetlands.zip"
    ok = download_file(
        "https://documentst.ecosphere.fws.gov/wetlands/data/State-Downloads/AL_geodatabase_wetlands.zip",
        dest_zip, desc="NWI AL wetlands GDB",
    )
    if ok and dest_zip.exists():
        gdb = next((AL / "nwi").glob("*.gdb"), None)
        if gdb is None:
            unzip(dest_zip, AL / "nwi")


# ---------------------------------------------------------------------------
# 2. USGS PAD-US 4.1 — Tennessee protected areas geodatabase
# ---------------------------------------------------------------------------
def download_padus():
    print("\n=== 2. USGS PAD-US 4.1 Protected Areas (AL) ===")
    dest_zip = AL / "padus" / "PADUS4_1_State_AL_GDB_KMZ.zip"
    # ScienceBase direct download token for the AL state GDB+KMZ file
    url = "https://www.sciencebase.gov/catalog/file/get/6759abcfd34edfeb8710a004?name=PADUS4_1_State_AL_GDB_KMZ.zip"
    ok = download_file(url, dest_zip, desc="PAD-US 4.1 AL GDB")
    if not ok:
        # Fallback: resolvable manager download URI
        ok = download_file(
            "https://sciencebase.usgs.gov/manager/download/cm8qkyg8p00120uphfpl821to",
            dest_zip, desc="PAD-US 4.1 AL GDB (fallback)",
        )
    if ok and dest_zip.exists():
        gdb = next((AL / "padus").rglob("*.gdb"), None)
        if gdb is None:
            unzip(dest_zip, AL / "padus")
        gdb = next((AL / "padus").rglob("*.gdb"), None)
        if gdb:
            print(f"    PAD-US GDB: {gdb.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# 3. NOAA Storm Events — recent years, filtered to Tennessee
# ---------------------------------------------------------------------------
def download_noaa_storms(start_year: int = 2015, end_year: int = 2025):
    print("\n=== 3. NOAA Storm Events (AL) ===")
    dest = AL / "noaa_storm_events.csv"
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [skip] {dest.name} already exists")
        return

    base = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
    print("  Listing NCEI storm-events directory …")
    try:
        idx = requests.get(base, headers=HEADERS, timeout=60).text
    except Exception as e:
        print(f"  [WARN] Could not list NCEI directory: {e}")
        return

    # Filenames look like: StormEvents_details-ftp_v1.0_d2023_c20240416.csv.gz
    pat = re.compile(r'StormEvents_details-ftp_v1\.0_d(\d{4})_c\d+\.csv\.gz')
    files = {}
    for m in pat.finditer(idx):
        yr = int(m.group(1))
        if start_year <= yr <= end_year:
            files[yr] = m.group(0)  # latest match per year wins

    if not files:
        print("  [WARN] No matching storm-events files found")
        return

    frames = []
    for yr in sorted(files):
        fname = files[yr]
        print(f"  Fetching {yr}: {fname}")
        try:
            r = requests.get(base + fname, headers=HEADERS, timeout=180)
            r.raise_for_status()
            with gzip.open(io.BytesIO(r.content), "rt") as gz:
                df = pd.read_csv(gz, low_memory=False)
            df = df[df["STATE"].astype(str).str.upper() == "TENNESSEE"]
            keep = [c for c in ["BEGIN_YEARMONTH", "EVENT_TYPE", "STATE",
                                "CZ_NAME", "CZ_TYPE", "DAMAGE_PROPERTY"]
                    if c in df.columns]
            frames.append(df[keep])
            print(f"    AL events {yr}: {len(df):,}")
        except Exception as e:
            print(f"  [WARN] {yr}: {e}")

    if frames:
        out = pd.concat(frames, ignore_index=True)
        out.to_csv(dest, index=False)
        print(f"    → {dest.name}  ({len(out):,} AL events, {start_year}-{end_year})")


# ---------------------------------------------------------------------------
# 4. FEMA National Risk Index — Tennessee county table
# ---------------------------------------------------------------------------
def download_fema_nri():
    print("\n=== 4. FEMA National Risk Index (AL counties) ===")
    dest = AL / "fema_nri" / "nri_al_counties.csv"
    if dest.exists() and dest.stat().st_size > 100:
        print(f"  [skip] {dest.name} already exists")
        return

    # FEMA retired the hazards.fema.gov static CSV downloads (now redirect to a
    # landing page). Pull AL counties from the official FEMA NRI ArcGIS service.
    base = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
            "National_Risk_Index_Counties/FeatureServer/0/query")
    print("  Querying FEMA NRI ArcGIS service for Tennessee …")
    try:
        rows, offset = [], 0
        while True:
            r = requests.get(base, params={
                "where": "STATEABBRV='TN'",
                "outFields": "*",
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": 1000,
                "f": "json",
            }, headers=HEADERS, timeout=120)
            r.raise_for_status()
            feats = r.json().get("features", [])
            if not feats:
                break
            rows.extend(a["attributes"] for a in feats)
            if len(feats) < 1000:
                break
            offset += len(feats)
        if rows:
            dest.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_csv(dest, index=False)
            print(f"    → {dest.name}  ({len(rows):,} AL county rows)")
        else:
            print("  [WARN] NRI service returned no AL rows")
    except Exception as e:
        print(f"  [WARN] NRI query failed: {e}")


# ---------------------------------------------------------------------------
# 5. Workforce Tennessee WARN notices (CSV feed)
# ---------------------------------------------------------------------------
def download_warn():
    print("\n=== 5. Workforce Tennessee WARN Notices ===")
    dest = AL / "warn" / "al_warn.csv"
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [skip] {dest.name} already exists")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = "https://workforce.tennessee.gov/documents/warn-list/"
    print("  Fetching WARN CSV feed …")
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        text = r.text
        # The feed returns CSV; sanity-check it parses
        df = pd.read_csv(io.StringIO(text))
        df.to_csv(dest, index=False)
        print(f"    → {dest.name}  ({len(df):,} WARN records)")
    except Exception as e:
        print(f"  [WARN] WARN feed failed: {e}")
        print("        Manual: https://workforce.tennessee.gov/warn-list/")


# ---------------------------------------------------------------------------
# Manual-only sources (no free statewide bulk endpoint)
# ---------------------------------------------------------------------------
def print_manual_sources():
    print("\n=== Manual-only sources (no free bulk download) ===")
    print("""
  Tennessee statewide parcel data:
    No free statewide parcel service (TX-style TNRIS) exists. Parcels are
    county-level, many behind paid portals. Options:
      - Per-county GIS: https://www.tennesseegis.com/ (county-by-county)
      - Regional aggregators (paid): id.land, Regrid
    The pipeline already sets parcel_acres = NaN and measures via Google Earth.

  Tennessee SOS business entity records:
    Search-only system, no clean bulk download (bulk page SSL chain is broken).
      - Entity search: https://arc-sos.state.al.us/CGI/CORPNAME.MBR/INPUT
      - Bulk licensing: contact AL SOS / OpenCorporates
    Used only for retirement-signal verification — query per-site as needed.

  ADECA OWR water-use certificates:
    eWater portal only, no public bulk export.
      - Portal: https://ewater.tennessee.gov/
      - Contact: water@adeca.tennessee.gov / (334) 242-5499
    Use per-site for water-rights due diligence on top candidates.
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("Tennessee — Extra Hazard / Environment / Connectivity Datasets")
    print("=" * 60)

    steps = [
        ("NWI Wetlands",        download_nwi),
        ("PAD-US Protected",    download_padus),
        ("NOAA Storm Events",   download_noaa_storms),
        ("FEMA NRI",            download_fema_nri),
        ("WARN Notices",        download_warn),
    ]
    failed = []
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            print(f"\n[ERROR] {name} failed: {e}")
            failed.append(name)

    print_manual_sources()

    print("\n" + "=" * 60)
    print("Extras download summary")
    print("=" * 60)
    for folder in sorted(AL.iterdir()):
        if not folder.is_dir():
            continue
        files = [f for f in folder.rglob("*") if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        print(f"  {folder.name:<22s}  {len(files):3d} files  {size/1e6:8.1f} MB")
    if failed:
        print(f"\nCompleted with errors in: {', '.join(failed)}")
    else:
        print("\nAll programmatic extras downloaded.")
    print("\nNote: NFHL flood, NSHM seismic, SSURGO soils are live per-site APIs")
    print("      already wired into filter_pipeline_al.py / score_and_export_al.py.")
