"""
fetch_epa_compliance.py
Fetches EPA ECHO compliance history, ICIS-AIR operating status, and
ICIS-NPDES permit status for every FRS-registered candidate site, via
EPA's live ECHO REST API, queried per-site by FRS Registry ID -- no bulk
national download needed since the pipeline only cares about a few
hundred specific facilities, and (as of this writing) no state's fork
of this pipeline, including this original Tennessee build, ever
actually wired these three sources up despite enrich_retirement.py
expecting them.

Verified API behavior (2026-07): the `p_frs` parameter takes exactly one
Registry ID per request -- a comma-separated multi-ID query silently
returns zero rows, so this queries one facility at a time. Each query is
a two-step flow (get_facilities returns a QueryID; get_qid returns the
actual rows), across three separate REST modules:
  - echo_rest_services  (general compliance: inspection recency, status)
  - air_rest_services   (ICIS-AIR: operating status, Major/Minor class)
  - cwa_rest_services   (ICIS-NPDES: permit status)

ICIS-NPDES's short status codes ("EFF"/"ADC"/"TRM") aren't exposed by
cwa_rest_services -- only the text description ("Effective"/"Terminated"/
etc.) is -- so this maps that text back to the codes enrich_retirement.py
already checks for, rather than changing its matching logic.

FAC_MAJOR_FLAG has no live equivalent in echo_rest_services for most
facilities (came back null even for a confirmed Title V major source in
testing), so it's derived here from ICIS-AIR's *current* classification
instead. That means enrich_retirement.py's Major-to-Minor "downgrade"
detection (which compares a historical major flag against the current
class) will never fire a downgrade -- there's no historical snapshot to
compare against -- but the flag itself is still an accurate "is this
currently a major source" signal on its own.

Output filenames match this repo's enrich_retirement.py exactly as
written (echo_al_facilities.csv / icis_air_al.csv / icis_npdes_al.csv --
a leftover "_al" naming convention traced back to this pipeline having
originally been forked from Alabama's; not changed here since that's a
separate, unrelated cleanup).

Run after filter_pipeline.py (needs site_id), before enrich_retirement.py.
"""
import time
from pathlib import Path

import pandas as pd
import geopandas as gpd
import requests

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "westvirginia" / "raw"
ECHO_DIR = RAW / "echo"
ICIS_DIR = RAW / "icis"
ECHO_DIR.mkdir(parents=True, exist_ok=True)
ICIS_DIR.mkdir(parents=True, exist_ok=True)

ECHO_DEST = ECHO_DIR / "echo_al_facilities.csv"
AIR_DEST = ICIS_DIR / "icis_air_al.csv"
NPDES_DEST = ICIS_DIR / "icis_npdes_al.csv"

if ECHO_DEST.exists() and AIR_DEST.exists() and NPDES_DEST.exists():
    print("[skip] EPA compliance CSVs already exist")
    raise SystemExit(0)

HEADERS = {"User-Agent": "DataCenterScreener/1.0 arthur.b.fok@gmail.com"}
BASE = "https://echodata.epa.gov/echo"

src_path = ROOT / "data" / "processed" / "candidates_filtered_wv.gpkg"
gdf = gpd.read_file(src_path)

reg_ids = sorted({
    str(sid).replace("FRS_", "").strip()
    for sid in gdf["site_id"].dropna()
    if str(sid).startswith("FRS_")
})
print(f"Querying EPA ECHO for {len(reg_ids)} FRS-registered candidates "
      f"({len(gdf) - len(reg_ids)} non-FRS sites, e.g. EIA power plants, "
      f"are skipped -- no registry ID to query against)...")


def _get_with_backoff(url, params, max_retries=5):
    delay = 3.0
    for attempt in range(max_retries):
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if r.status_code == 429:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"gave up after {max_retries} retries on 429s: {url}")


def query_facility(module, reg_id):
    try:
        r1 = _get_with_backoff(f"{BASE}/{module}.get_facilities",
                                {"output": "JSON", "p_frs": reg_id, "responseset": 1})
        d1 = r1.json().get("Results", {})
        if "QueryID" not in d1 or d1.get("QueryRows") in (None, "0", 0):
            return []
        r2 = _get_with_backoff(f"{BASE}/{module}.get_qid",
                                {"qid": d1["QueryID"], "output": "JSON"})
        return r2.json().get("Results", {}).get("Facilities", []) or []
    except Exception as e:
        print(f"  [WARN] {module} query failed for {reg_id}: {e}")
        return []


NPDES_STATUS_TO_CODE = {
    "effective": "EFF",
    "administratively continued": "ADC",
    "admin continued": "ADC",
    "terminated": "TRM",
    "expired": "TRM",  # no longer an active discharge permit -- same retirement signal as terminated
}

# Pass 1: ICIS-AIR -- also builds the reg_id -> "currently major?" lookup for ECHO's FAC_MAJOR_FLAG
air_rows = []
major_by_id = {}
for i, reg_id in enumerate(reg_ids):
    for f in query_facility("air_rest_services", reg_id):
        cls = f.get("AIRClassification") or ""
        air_rows.append({
            "REGISTRY_ID": f.get("RegistryID", reg_id),
            "AIR_POLLUTANT_CLASS_CODE": cls,
            "AIR_POLLUTANT_CLASS_DESC": cls,
            "AIR_OPERATING_STATUS_CODE": f.get("AIRStatus") or "",
            "AIR_OPERATING_STATUS_DESC": f.get("AIRStatus") or "",
        })
        if cls == "Major Emissions":
            major_by_id[reg_id] = True
    time.sleep(0.6)
    if (i + 1) % 25 == 0:
        print(f"  ICIS-AIR: {i+1}/{len(reg_ids)} sites queried...")

# Pass 2: general ECHO compliance
echo_rows = []
for i, reg_id in enumerate(reg_ids):
    for f in query_facility("echo_rest_services", reg_id):
        echo_rows.append({
            "REGISTRY_ID": f.get("RegistryID", reg_id),
            "FAC_DAYS_LAST_INSPECTION": f.get("FacDaysLastInspection"),
            "FAC_COMPLIANCE_STATUS": f.get("FacComplianceStatus") or "",
            "FAC_MAJOR_FLAG": "Y" if major_by_id.get(reg_id) else "",
        })
    time.sleep(0.6)
    if (i + 1) % 25 == 0:
        print(f"  ECHO: {i+1}/{len(reg_ids)} sites queried...")

# Pass 3: ICIS-NPDES water permit status
npdes_rows = []
for i, reg_id in enumerate(reg_ids):
    for f in query_facility("cwa_rest_services", reg_id):
        desc = (f.get("CWPPermitStatusDesc") or "").strip()
        code = NPDES_STATUS_TO_CODE.get(desc.lower(), (desc.upper()[:3] or "UNK"))
        npdes_rows.append({"FACILITY_UIN": reg_id, "PERMIT_STATUS_CODE": code})
    time.sleep(0.6)
    if (i + 1) % 25 == 0:
        print(f"  ICIS-NPDES: {i+1}/{len(reg_ids)} sites queried...")

pd.DataFrame(echo_rows).to_csv(ECHO_DEST, index=False)
pd.DataFrame(air_rows).to_csv(AIR_DEST, index=False)
pd.DataFrame(npdes_rows).to_csv(NPDES_DEST, index=False)
print(f"\nECHO: {len(echo_rows)} rows -> {ECHO_DEST.name}")
print(f"ICIS-AIR: {len(air_rows)} rows -> {AIR_DEST.name}")
print(f"ICIS-NPDES: {len(npdes_rows)} rows -> {NPDES_DEST.name}")
