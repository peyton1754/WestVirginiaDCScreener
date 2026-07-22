"""
enrich_retirement_al.py  (v3 — permit trajectories, ownership, ready-for-reuse)
Computes composite retirement confidence for West Virginia candidates.

Signal categories:
  1. EPA TRI facility closed flag
  2. EPA ECHO inspection recency + compliance status
  3. ICIS-AIR permit trajectory (Major→Minor downgrade, Permanently Closed status)
  4. ICIS-NPDES permit terminations (terminated/expired water discharge permits)
  5. Entity ownership keywording (trust, redevelopment, holding, remediation)
  6. EPA "Ready for Reuse" — SEMS NPL-deleted sites + ACRES brownfield enrollment
  7. WorkForce West Virginia WARN Act (strict matching)
  8. Active operator name blocklist

Run after enrich_candidates_al.py, before score_and_export_al.py.
"""

import re
import time
import warnings
import json
import numpy as np
import pandas as pd
import geopandas as gpd
import requests
from pathlib import Path
from difflib import get_close_matches

warnings.filterwarnings("ignore")

ROOT     = Path(__file__).parent
PROC_DIR = ROOT / "data" / "processed"
AL_RAW   = ROOT / "data" / "westvirginia" / "raw"
STATE    = "wv"

HEADERS = {"User-Agent": "DataCenterScreener/1.0"}

print("=" * 60)
print("West Virginia Retirement Confidence Enrichment (v3)")
print("=" * 60)

# ---------------------------------------------------------------------------
# Load candidates
# ---------------------------------------------------------------------------
filtered_path = PROC_DIR / f"candidates_filtered_{STATE}.gpkg"
cands = gpd.read_file(filtered_path)
print(f"\nLoaded {len(cands)} candidates from {filtered_path.name}")

cands_wgs = cands.to_crs("EPSG:4326")


# ===================================================================
# 1. EPA TRI — facility closed flag
# ===================================================================
print("\n" + "=" * 60)
print("1. EPA TRI — Facility Closed Flag")
print("=" * 60)

cands["tri_last_year"] = ""
cands["tri_closed"] = ""

tri_lookup = {}

def _fetch_all(base_url, batch_size=10000):
    rows, offset = [], 0
    while True:
        url = f"{base_url}/rows/{offset}:{offset + batch_size}/json"
        r = requests.get(url, headers=HEADERS, timeout=120)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows.extend(data)
        if len(data) < batch_size:
            break
        offset += batch_size
    return rows

print("  Fetching WV TRI facility table …")
try:
    fac_rows = _fetch_all("https://data.epa.gov/efservice/tri_facility/state_abbr/WV")
    if fac_rows:
        fac_df = pd.DataFrame(fac_rows)
        fac_df["_name"] = fac_df["facility_name"].astype(str).str.upper().str.strip()
        fac_df["_city"] = fac_df["city_name"].astype(str).str.upper().str.strip()
        fac_df["_closed"] = fac_df["fac_closed_ind"].astype(str) == "1"
        print(f"  TRI facilities: {len(fac_df):,} ({fac_df['_closed'].sum()} marked closed)")
        for _, row in fac_df.iterrows():
            tri_lookup[(row["_name"], row["_city"])] = {"closed": row["_closed"]}
except Exception as e:
    print(f"  [WARN] TRI fetch failed: {e}")

if tri_lookup:
    tri_names = list(set(k[0] for k in tri_lookup))
    n_matched = 0
    for i in range(len(cands)):
        cand_name = str(cands.iloc[i]["Plant_Name"]).upper().strip()
        cand_city = str(cands.iloc[i].get("City", "")).upper().strip()
        match = None
        if (cand_name, cand_city) in tri_lookup:
            match = tri_lookup[(cand_name, cand_city)]
        else:
            city_names = [k[0] for k in tri_lookup if k[1] == cand_city]
            if city_names:
                m = get_close_matches(cand_name, city_names, n=1, cutoff=0.45)
                if m:
                    match = tri_lookup.get((m[0], cand_city))
            if not match:
                m = get_close_matches(cand_name, tri_names, n=1, cutoff=0.55)
                if m:
                    match = next(v for k, v in tri_lookup.items() if k[0] == m[0])
        if match:
            cands.at[cands.index[i], "tri_closed"] = "YES" if match["closed"] else "NO"
            n_matched += 1
    print(f"  Matched {n_matched}/{len(cands)} | Closed: {(cands['tri_closed']=='YES').sum()}")


# ===================================================================
# 2. EPA ECHO — inspection recency + compliance
# ===================================================================
print("\n" + "=" * 60)
print("2. EPA ECHO — Inspection & Compliance")
print("=" * 60)

cands["echo_days_since_inspection"] = np.nan
cands["echo_compliance_status"] = ""
cands["echo_major_flag"] = ""

echo_csv = AL_RAW / "echo" / "echo_al_facilities.csv"
echo_by_id = {}
if echo_csv.exists():
    echo = pd.read_csv(echo_csv, dtype=str, low_memory=False)
    echo["FAC_DAYS_LAST_INSPECTION"] = pd.to_numeric(echo["FAC_DAYS_LAST_INSPECTION"], errors="coerce")
    for _, row in echo.iterrows():
        rid = str(row.get("REGISTRY_ID", "")).strip()
        if rid:
            echo_by_id[rid] = {
                "days": row["FAC_DAYS_LAST_INSPECTION"],
                "compliance": str(row.get("FAC_COMPLIANCE_STATUS", "")),
                "major": str(row.get("FAC_MAJOR_FLAG", "")),
            }
    n_echo = 0
    for i in range(len(cands)):
        sid = str(cands.iloc[i].get("site_id", "")).replace("FRS_", "")
        if sid in echo_by_id:
            info = echo_by_id[sid]
            if pd.notna(info["days"]):
                cands.at[cands.index[i], "echo_days_since_inspection"] = info["days"]
            cands.at[cands.index[i], "echo_compliance_status"] = info["compliance"]
            cands.at[cands.index[i], "echo_major_flag"] = info["major"]
            n_echo += 1
    print(f"  Matched {n_echo}/{len(cands)} to ECHO | "
          f"Major: {(cands['echo_major_flag']=='Y').sum()} | "
          f"Inspected <1yr: {(cands['echo_days_since_inspection'] <= 365).sum()} | "
          f"Lapsed >5yr: {(cands['echo_days_since_inspection'] > 1825).sum()}")
else:
    print("  [skip] ECHO AL CSV not found")


# ===================================================================
# 3. ICIS-AIR — permit trajectory (Major→Minor, Permanently Closed)
# ===================================================================
print("\n" + "=" * 60)
print("3. ICIS-AIR — Permit Trajectory")
print("=" * 60)

cands["air_source_class"] = ""
cands["air_operating_status"] = ""

air_csv = AL_RAW / "icis" / "icis_air_al.csv"
if air_csv.exists():
    air = pd.read_csv(air_csv, dtype=str, low_memory=False)
    print(f"  Loaded {len(air)} AL air facilities")

    # Build registry ID lookup
    air_by_id = {}
    for _, row in air.iterrows():
        rid = str(row.get("REGISTRY_ID", "")).strip()
        if rid:
            air_by_id[rid] = {
                "class_code": str(row.get("AIR_POLLUTANT_CLASS_CODE", "")),
                "class_desc": str(row.get("AIR_POLLUTANT_CLASS_DESC", "")),
                "status_code": str(row.get("AIR_OPERATING_STATUS_CODE", "")),
                "status_desc": str(row.get("AIR_OPERATING_STATUS_DESC", "")),
            }

    n_air = 0
    for i in range(len(cands)):
        sid = str(cands.iloc[i].get("site_id", "")).replace("FRS_", "")
        if sid in air_by_id:
            info = air_by_id[sid]
            cands.at[cands.index[i], "air_source_class"] = info["class_desc"]
            cands.at[cands.index[i], "air_operating_status"] = info["status_desc"]
            n_air += 1

    n_closed = (cands["air_operating_status"] == "Permanently Closed").sum()
    n_major = (cands["air_source_class"] == "Major Emissions").sum()
    n_minor = cands["air_source_class"].isin(["Minor Emissions", "Synthetic Minor Emissions"]).sum()

    # Detect Title V downgrades: site was Major in ECHO but now Minor/SynMin in ICIS-AIR
    # (ECHO FAC_MAJOR_FLAG=Y means historically major; ICIS-AIR shows current classification)
    n_downgrade = 0
    for i in range(len(cands)):
        was_major = cands.iloc[i].get("echo_major_flag") == "Y"
        now_class = str(cands.iloc[i].get("air_source_class", ""))
        if was_major and now_class in ("Minor Emissions", "Synthetic Minor Emissions"):
            n_downgrade += 1

    print(f"  Matched {n_air}/{len(cands)} | Permanently Closed: {n_closed} | "
          f"Major: {n_major} | Minor/SynMin: {n_minor}")
    print(f"  Title V downgrades (was Major, now Minor/SynMin): {n_downgrade}")
else:
    print("  [skip] ICIS-AIR AL CSV not found — run extraction")


# ===================================================================
# 4. ICIS-NPDES — permit terminations
# ===================================================================
print("\n" + "=" * 60)
print("4. ICIS-NPDES — Water Permit Terminations")
print("=" * 60)

cands["npdes_terminated"] = ""
cands["npdes_permit_status"] = ""

npdes_csv = AL_RAW / "icis" / "icis_npdes_al.csv"
if npdes_csv.exists():
    npdes = pd.read_csv(npdes_csv, dtype=str, low_memory=False)
    print(f"  Loaded {len(npdes)} AL NPDES permits")

    # Match by FACILITY_UIN (= FRS REGISTRY_ID in some cases) or spatial proximity
    npdes_by_uin = {}
    for _, row in npdes.iterrows():
        uin = str(row.get("FACILITY_UIN", "")).strip()
        status = str(row.get("PERMIT_STATUS_CODE", ""))
        if uin:
            if uin not in npdes_by_uin:
                npdes_by_uin[uin] = []
            npdes_by_uin[uin].append(status)

    n_npdes = 0
    for i in range(len(cands)):
        sid = str(cands.iloc[i].get("site_id", "")).replace("FRS_", "")
        if sid in npdes_by_uin:
            statuses = npdes_by_uin[sid]
            has_terminated = "TRM" in statuses
            has_effective = "EFF" in statuses or "ADC" in statuses
            cands.at[cands.index[i], "npdes_terminated"] = "YES" if has_terminated and not has_effective else ""
            cands.at[cands.index[i], "npdes_permit_status"] = "; ".join(sorted(set(statuses)))
            n_npdes += 1

    n_term = (cands["npdes_terminated"] == "YES").sum()
    print(f"  Matched {n_npdes}/{len(cands)} | All permits terminated (no active): {n_term}")
else:
    print("  [skip] ICIS-NPDES AL CSV not found")


# ===================================================================
# 5. Entity ownership keywording
# ===================================================================
print("\n" + "=" * 60)
print("5. Entity Ownership Keywording")
print("=" * 60)

# Check ECHO facility names for ownership transition keywords
REDEV_KEYWORDS = re.compile(
    r"\b(TRUST|REDEVELOPMENT|HOLDING\s*(CO|COMPANY|CORP)|"
    r"REMEDIAT|LAND\s*BANK|CONSERVATION|RECEIVER|LIQUIDAT|"
    r"FORMERLY|FKA\b|F/K/A|DBA\b|SUCCESSOR|ESTATE\s+OF|"
    r"ABANDONED|DEFUNCT|CLOSED)\b", re.IGNORECASE
)

cands["ownership_keyword"] = ""
n_redev = 0
for i in range(len(cands)):
    sid = str(cands.iloc[i].get("site_id", "")).replace("FRS_", "")
    # Check ECHO facility name for ownership transition keywords
    echo_name = ""
    if sid in echo_by_id:
        # Get the ECHO facility name from the full data
        echo_row = echo[echo["REGISTRY_ID"] == sid]
        if len(echo_row):
            echo_name = str(echo_row.iloc[0].get("FAC_NAME", ""))

    plant_name = str(cands.iloc[i].get("Plant_Name", ""))
    combined = f"{plant_name} {echo_name}"

    match = REDEV_KEYWORDS.search(combined)
    if match:
        cands.at[cands.index[i], "ownership_keyword"] = match.group(0).strip()
        n_redev += 1

print(f"  Ownership transition keywords found: {n_redev}/{len(cands)}")
if n_redev:
    for i in range(len(cands)):
        kw = cands.iloc[i]["ownership_keyword"]
        if kw:
            print(f"    {cands.iloc[i]['Plant_Name']:40s} → '{kw}'")


# ===================================================================
# 6. EPA "Ready for Reuse" — SEMS NPL-deleted + ACRES brownfield
# ===================================================================
print("\n" + "=" * 60)
print("6. EPA Ready-for-Reuse Cross-Reference")
print("=" * 60)

cands["epa_reuse_status"] = ""

# SEMS: "DELETED FROM THE FINAL NPL" = cleanup complete, ready for reuse
sems_path = AL_RAW / "brownfields" / "epa_sems_al.geojson"
sems_reuse_ids = set()
if sems_path.exists():
    with open(sems_path) as f:
        sems_data = json.load(f)
    for feat in sems_data.get("features", []):
        p = feat["properties"]
        status = str(p.get("ACTIVE_STATUS", "")).upper()
        if "DELETED" in status:
            sems_reuse_ids.add(str(p.get("REGISTRY_ID", "")))
    print(f"  SEMS NPL-deleted (cleanup complete): {len(sems_reuse_ids)} sites")

# ACRES: any brownfield enrollment = site is in EPA redevelopment pipeline
acres_path = AL_RAW / "brownfields" / "epa_acres_al.geojson"
acres_ids = set()
if acres_path.exists():
    with open(acres_path) as f:
        acres_data = json.load(f)
    for feat in acres_data.get("features", []):
        p = feat["properties"]
        rid = str(p.get("REGISTRY_ID", ""))
        if rid:
            acres_ids.add(rid)
    print(f"  ACRES brownfield enrollments: {len(acres_ids)} sites")

# RCRA Inactive with cleanup interest
rcra_path = AL_RAW / "brownfields" / "epa_rcra_inactive_al.geojson"
rcra_inactive_ids = set()
if rcra_path.exists():
    with open(rcra_path) as f:
        rcra_data = json.load(f)
    for feat in rcra_data.get("features", []):
        p = feat["properties"]
        rid = str(p.get("REGISTRY_ID", ""))
        if rid:
            rcra_inactive_ids.add(rid)

# Match to candidates
n_reuse = 0
for i in range(len(cands)):
    sid = str(cands.iloc[i].get("site_id", "")).replace("FRS_", "")
    statuses = []
    if sid in sems_reuse_ids:
        statuses.append("SUPERFUND_CLEANUP_COMPLETE")
    if sid in acres_ids:
        statuses.append("ACRES_BROWNFIELD_ENROLLED")
    if statuses:
        cands.at[cands.index[i], "epa_reuse_status"] = "; ".join(statuses)
        n_reuse += 1

print(f"  Matched to reuse datasets: {n_reuse}/{len(cands)}")


# ===================================================================
# 7. WARN Act — strict matching (cutoff 0.80)
# ===================================================================
print("\n" + "=" * 60)
print("7. WorkForce West Virginia WARN Act (strict)")
print("=" * 60)

cands["warn_match"] = ""
cands["warn_date"] = ""

warn_path = AL_RAW / "warn" / "al_warn.csv"
if warn_path.exists():
    warn = pd.read_csv(warn_path, dtype=str, header=None,
                       names=["warn_id", "type", "planned_date", "report_date",
                              "company", "city", "employees", "seq"])
    warn["_company"] = warn["company"].astype(str).str.upper().str.strip()
    warn["_city"] = warn["city"].astype(str).str.upper().str.strip()
    print(f"  Loaded {len(warn):,} WARN records")

    n_warn = 0
    for i in range(len(cands)):
        cand_name = str(cands.iloc[i]["Plant_Name"]).upper().strip()
        cand_city = str(cands.iloc[i].get("City", "")).upper().strip()
        city_warn = warn[warn["_city"] == cand_city] if cand_city else warn
        if len(city_warn) == 0:
            continue
        matches = get_close_matches(cand_name, city_warn["_company"].tolist(), n=1, cutoff=0.80)
        if matches:
            row = city_warn[city_warn["_company"] == matches[0]].iloc[0]
            cands.at[cands.index[i], "warn_match"] = str(row["company"])
            cands.at[cands.index[i], "warn_date"] = str(row["planned_date"])
            n_warn += 1
    print(f"  WARN matches: {n_warn}/{len(cands)}")
else:
    print("  [skip] WARN data not found")


# ===================================================================
# 8. Composite Retirement Confidence
# ===================================================================
print("\n" + "=" * 60)
print("8. Composite Retirement Confidence")
print("=" * 60)

ACTIVE_OPERATOR_PATTERNS = re.compile(
    r"\b(EXXON|MOBIL|CHEVRON|SHELL|MARATHON|DOW\b|DUPONT|"
    r"HUNTSMAN|CELANESE|AIR\s*LIQUIDE|AIR\s*PRODUCTS|LINDE\b|PRAXAIR|"
    r"NUCOR|GERDAU|COMMERCIAL\s*METALS|SSAB|OUTOKUMPU|"
    r"PRYSMIAN|MCWANE|MARTIN\s*MARIETTA|CEMEX|HOLCIM|CRH\b|QUIKRETE|"
    r"TENNESSEE\s*POWER|SOUTHERN\s*COMPANY|TVA\b|"
    r"TORAY|SOLVAY|STYROLUTION|POLYPLEX|OCI\s*TENNESSEE|"
    r"ARKEMA|BERMCO|GEORGIA.PACIFIC|INTERNATIONAL\s*PAPER|"
    r"BASF|EVONIK|CARPENTER\s*TECH|HARSCO|VULCAN|"
    r"KOPPERS|AMVAC|TATE\s*&\s*LYLE)"
)

retirement_confidence = []
retirement_signals = []

for i in range(len(cands)):
    signals = []

    # Source-based
    src = str(cands.iloc[i].get("source", ""))
    if src == "EIA860":
        signals.append("EIA_RETIRED_GENERATOR")
    if src == "TRI":
        signals.append("TRI_STOPPED_REPORTING")

    # TRI closed flag
    if str(cands.iloc[i].get("tri_closed", "")) == "YES":
        signals.append("TRI_CLOSED_FLAG")

    # ECHO inspection recency
    echo_days = cands.iloc[i].get("echo_days_since_inspection", np.nan)
    if pd.notna(echo_days):
        d = float(echo_days)
        if d <= 365:
            signals.append("⚠_RECENTLY_INSPECTED")
        elif d > 3650:
            signals.append("INSPECTION_LAPSED_10YR")
        elif d > 1825:
            signals.append("INSPECTION_LAPSED_5YR")

    # ECHO compliance inactive
    if str(cands.iloc[i].get("echo_compliance_status", "")) == "Inactive":
        signals.append("ECHO_COMPLIANCE_INACTIVE")

    # ICIS-AIR: Permanently Closed status (direct from EPA air program)
    if str(cands.iloc[i].get("air_operating_status", "")) == "Permanently Closed":
        signals.append("AIR_PERMIT_PERMANENTLY_CLOSED")

    # ICIS-AIR: Title V downgrade (was Major, now Minor/SynMin)
    was_major = cands.iloc[i].get("echo_major_flag") == "Y"
    now_class = str(cands.iloc[i].get("air_source_class", ""))
    if was_major and now_class in ("Minor Emissions", "Synthetic Minor Emissions"):
        signals.append("TITLE_V_DOWNGRADED")

    # NPDES: all permits terminated (no active water discharge)
    if str(cands.iloc[i].get("npdes_terminated", "")) == "YES":
        signals.append("NPDES_ALL_TERMINATED")

    # Ownership keywords (trust, redevelopment, remediation, etc.)
    if str(cands.iloc[i].get("ownership_keyword", "")):
        signals.append("OWNERSHIP_TRANSITION")

    # EPA Ready-for-Reuse
    reuse = str(cands.iloc[i].get("epa_reuse_status", ""))
    if "SUPERFUND_CLEANUP_COMPLETE" in reuse:
        signals.append("SUPERFUND_CLEANUP_COMPLETE")
    if "ACRES_BROWNFIELD_ENROLLED" in reuse:
        signals.append("ACRES_BROWNFIELD")

    # WARN Act
    if str(cands.iloc[i].get("warn_match", "")) not in ("", "nan"):
        signals.append("WARN_NOTICE_MATCH")

    # Air permit history
    if cands.iloc[i].get("has_air_permit") is True:
        signals.append("HAD_AIR_PERMIT")

    # Active operator name warning
    plant_name = str(cands.iloc[i].get("Plant_Name", "")).upper()
    if ACTIVE_OPERATOR_PATTERNS.search(plant_name):
        signals.append("⚠_ACTIVE_OPERATOR_NAME")

    # ICIS-AIR: currently operating
    if str(cands.iloc[i].get("air_operating_status", "")) == "Operating":
        signals.append("⚠_AIR_PERMIT_OPERATING")

    # Compute confidence
    negative = [s for s in signals if s.startswith("⚠")]
    positive = [s for s in signals if not s.startswith("⚠")]

    # Hard-exclusion signals: if ANY of these are present, the site is
    # definitively still operating and cannot be a brownfield candidate
    hard_exclude = [s for s in negative if s in (
        "⚠_AIR_PERMIT_OPERATING",      # EPA air program says currently operating
        "⚠_RECENTLY_INSPECTED",         # EPA inspected within 1 year
    )]

    # Not all positive signals are equally strong evidence of retirement.
    # STRONG signals are each independently meaningful (a specific EIA/TRI/WARN/
    # Superfund/ownership record directly asserting closure or transition).
    # WEAK signals (permit lapse, inspection lapse, permit termination, ACRES
    # enrollment, etc.) each just describe a specific permit's lifecycle or a
    # lapse in EPA attention -- a small, fully compliant, currently ACTIVE
    # facility can accumulate several of these on paper with no bearing on
    # whether it's actually vacant today. Verified against Kentucky's
    # "Precision Steel LLC" (confirmed active Harper Industries subsidiary,
    # scored HIGH pre-fix off 3 weak signals alone) and Alabama's "Merichem
    # Chemicals" (same pattern). Requiring at least one strong signal (or many
    # independent weak ones) before HIGH/VERY_HIGH fixes this class of false
    # positive.
    #
    # KNOWN REMAINING GAP: this does not catch a genuinely-closed site that
    # was later re-occupied by a new, unrelated active tenant -- e.g.
    # Louisiana's "Fuel Solutions LLC", which scored VERY_HIGH off a real
    # TRI_CLOSED_FLAG (an old paper mill's real, historical closure) plus 4
    # weak signals, and stays VERY_HIGH under this fix too, because
    # TRI_CLOSED_FLAG is genuinely strong evidence -- just stale. Catching
    # re-occupancy needs a live current-operating-status check (e.g. a
    # state business-registry active-status lookup) this pipeline doesn't
    # have yet; that's separate follow-up work, not something a signal
    # re-weighting can fix.
    STRONG_SIGNALS = {
        "EIA_RETIRED_GENERATOR",
        "TRI_CLOSED_FLAG",
        "SUPERFUND_CLEANUP_COMPLETE",
        "WARN_NOTICE_MATCH",
        "OWNERSHIP_TRANSITION",
        "TITLE_V_DOWNGRADED",
    }
    n_strong = sum(1 for s in positive if s in STRONG_SIGNALS)
    n_weak = len(positive) - n_strong

    if hard_exclude:
        confidence = "EXCLUDED"
    elif negative:
        confidence = "ACTIVE_WARNING"
    elif n_strong >= 2:
        confidence = "VERY_HIGH"
    elif n_strong == 1 and n_weak >= 2:
        confidence = "VERY_HIGH"
    elif n_strong == 1:
        confidence = "HIGH"
    elif n_weak >= 4:
        confidence = "HIGH"
    elif n_weak >= 2:
        confidence = "MEDIUM"
    elif n_weak >= 1:
        confidence = "LOW"
    else:
        confidence = "UNVERIFIED"

    retirement_confidence.append(confidence)
    retirement_signals.append("; ".join(signals))

cands["retirement_confidence"] = retirement_confidence
cands["retirement_signals"] = retirement_signals

n_excluded = sum(1 for c in retirement_confidence if c == "EXCLUDED")
print(f"\n  HARD-EXCLUDED (definitively active): {n_excluded}")

print("\nRetirement confidence distribution (remaining):")
for level in ["VERY_HIGH", "HIGH", "MEDIUM", "LOW", "ACTIVE_WARNING", "UNVERIFIED"]:
    n = sum(1 for c in retirement_confidence if c == level)
    if n:
        print(f"  {level:18s}: {n}")
        if level in ("VERY_HIGH", "HIGH"):
            for j in range(len(cands)):
                if retirement_confidence[j] == level:
                    print(f"    {cands.iloc[j]['Plant_Name']:45s} {retirement_signals[j]}")

n_warn_total = sum(1 for c in retirement_confidence if c == "ACTIVE_WARNING")
if n_warn_total:
    # Show breakdown of warning reasons
    from collections import Counter
    warn_reasons = Counter()
    for j in range(len(cands)):
        if retirement_confidence[j] == "ACTIVE_WARNING":
            for s in retirement_signals[j].split("; "):
                if s.startswith("⚠"):
                    warn_reasons[s] += 1
    print(f"\n  ACTIVE_WARNING reasons ({n_warn_total} sites):")
    for reason, count in warn_reasons.most_common():
        print(f"    {reason}: {count}")


# ===================================================================
# Save
# ===================================================================
print("\n" + "=" * 60)
print("Saving enriched candidates")
print("=" * 60)

out_gpkg = PROC_DIR / f"candidates_enriched_{STATE}.gpkg"
out_csv  = PROC_DIR / f"candidates_enriched_{STATE}.csv"
cands.to_file(out_gpkg, driver="GPKG")
cands.drop(columns="geometry").to_csv(out_csv, index=False)
print(f"  {out_gpkg}")
print(f"  {out_csv}")
print(f"\n{len(cands)} candidates enriched with retirement confidence (v3).")
print("Run score_and_export_al.py next.")
