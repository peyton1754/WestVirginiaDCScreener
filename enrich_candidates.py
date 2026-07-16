"""
enrich_candidates_al.py
Generates a manual-verification report for filtered West Virginia candidates.

For each site produces:
  - Google Maps satellite link (visual site check)
  - EPA ECHO facility report link (permit/inspection history)
  - WVDEP facility search link (state permit status)
  - EPA EnviroMapper link (regulatory overview)

Output:
  data/processed/candidates_enriched_al.gpkg
  outputs/csv/verification_links_al.csv

Run: python3 enrich_candidates_al.py
"""

import re
import numpy as np
import pandas as pd
import geopandas as gpd
from pathlib import Path
from urllib.parse import quote

ROOT     = Path(__file__).parent
PROC_DIR = ROOT / "data" / "processed"
STATE    = "wv"
OUT_DIR  = ROOT / "outputs" / "csv"
OUT_DIR.mkdir(parents=True, exist_ok=True)

print("=" * 60)
print("Enrich Candidates — West Virginia Manual Verification Report")
print("=" * 60)

gdf = gpd.read_file(PROC_DIR / f"candidates_filtered_{STATE}.gpkg")
wgs = gdf.to_crs("EPSG:4326")
print(f"\nLoaded {len(gdf)} filtered candidates")

# ---------------------------------------------------------------------------
# Additional name-pattern exclusions
# ---------------------------------------------------------------------------
EXCLUDE_PATTERNS = [
    (r"\basphalt\b",           "Asphalt plant — commodity, almost always active"),
    (r"\bpaving\b",            "Paving company — commodity, almost always active"),
    (r"\bbituminous\b",        "Bituminous plant — commodity operation"),
    (r"\bconstruction\s+co\b", "Construction company — not an industrial brownfield"),
    (r"\bcontracting\b",       "Contracting company — not an industrial brownfield"),
    (r"\bcontractor\b",        "Contractor — not an industrial brownfield"),
    (r"\bpallet\b",            "Pallet company — low-barrier, often still operating"),
    (r"\bportable\b",          "Portable equipment — no fixed brownfield site"),
]

exclude_mask  = pd.Series(False, index=gdf.index)
exclude_reason = pd.Series("", index=gdf.index)

for pattern, reason in EXCLUDE_PATTERNS:
    matched = gdf["Plant_Name"].str.lower().str.contains(pattern, regex=True, na=False)
    newly_matched = matched & ~exclude_mask
    if newly_matched.any():
        names = gdf.loc[newly_matched, "Plant_Name"].tolist()
        print(f"\n  Pattern '{pattern}' excludes {newly_matched.sum()} site(s):")
        for n in names:
            print(f"    - {n}")
    exclude_reason = exclude_reason.where(~newly_matched, reason)
    exclude_mask  |= matched

n_excluded = exclude_mask.sum()
print(f"\n  {n_excluded} site(s) excluded by name patterns")

gdf_clean = gdf[~exclude_mask].copy().reset_index(drop=True)
wgs_clean = wgs[~exclude_mask].copy().reset_index(drop=True)
print(f"  {len(gdf_clean)} candidates remaining")

# ---------------------------------------------------------------------------
# Build verification link table
# ---------------------------------------------------------------------------
print("\nGenerating verification links …")

rows = []
for i, (_, row) in enumerate(gdf_clean.iterrows()):
    lat = wgs_clean.at[i, "geometry"].y
    lon = wgs_clean.at[i, "geometry"].x
    name    = str(row.get("Plant_Name", ""))
    address = str(row.get("Street_Address", ""))
    city    = str(row.get("City", ""))
    reg_id  = str(row.get("site_id", "")).replace("FRS_", "")
    source  = str(row.get("source", ""))

    maps_url = (
        f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},18z/data=!3m1!1e3"
    )

    addr_query = quote(f"{name} {address} {city} WV")
    maps_search = f"https://www.google.com/maps/search/{addr_query}"

    echo_url = (
        f"https://echo.epa.gov/detailed-facility-report?fid={reg_id}"
        if source == "FRS" else ""
    )

    enviromapper_url = (
        f"https://enviro.epa.gov/envirofacts/multisystem/facility/{reg_id}"
        if source == "FRS" else ""
    )

    # WVDEP E-Permitting — permit search by facility name/location
    adem_url = "https://dep.wv.gov/SearchDEP/Pages/E-Permitting-Application-Search.aspx"

    earth_url = (
        f"https://earth.google.com/web/@{lat:.6f},{lon:.6f},300a,500d,35y,0h,0t,0r"
    )

    rows.append({
        "rank":             i + 1,
        "site_id":          row.get("site_id", ""),
        "Plant_Name":       name,
        "City":             city,
        "County":           row.get("County", ""),
        "brownfield_type":  row.get("brownfield_type", ""),
        "naics_code":       row.get("naics_code", ""),
        "Latitude":         round(lat, 6),
        "Longitude":        round(lon, 6),
        "estimated_acres":  "",
        "google_satellite": maps_url,
        "google_earth":     earth_url,
        "google_search":    maps_search,
        "echo_report":      echo_url,
        "enviromapper":     enviromapper_url,
        "adem_search":      adem_url,
        "verify_checklist": (
            "1) Satellite: empty/demolished? Overgrown? Cleared pad? "
            "2) Google Earth: use polygon tool to measure parcel/site area — need ≥50 ac. "
            "3) ECHO: last inspection date, permit status. "
            "4) WVDEP E-Permitting: any active air/water permits? "
            "5) Street View: fencing, no-trespass signs, derelict equipment?"
        ),
        "status":           "",
        "notes":            "",
    })

links_df = pd.DataFrame(rows)
links_path = OUT_DIR / f"verification_links_{STATE}.csv"
links_df.to_csv(links_path, index=False)
print(f"  Saved: {links_path}")
print(f"\n  Open this CSV in Excel/Sheets and work through each row.")
print(f"  Fill 'status' column: CONFIRMED_VACANT | ACTIVE | NEEDS_MORE_INFO")
print(f"  Takes ~1–2 min per site = {len(gdf_clean) * 1.5:.0f}–{len(gdf_clean) * 2:.0f} min total")

# ---------------------------------------------------------------------------
# Save enriched candidate file
# ---------------------------------------------------------------------------
gdf_clean.to_file(PROC_DIR / f"candidates_enriched_{STATE}.gpkg", driver="GPKG")
gdf_clean.drop(columns="geometry").to_csv(
    PROC_DIR / f"candidates_enriched_{STATE}.csv", index=False
)
print(f"\nSaved enriched candidates ({len(gdf_clean)} sites):")
print(f"  {PROC_DIR / f'candidates_enriched_{STATE}.gpkg'}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("Summary")
print("=" * 60)
print(f"  Input:     {len(gdf)} candidates")
print(f"  Excluded:  {n_excluded} (name-pattern: asphalt/contractor/pallet/portable)")
if n_excluded:
    for _, row in gdf[exclude_mask].iterrows():
        reason = exclude_reason[row.name]
        print(f"    - {row['Plant_Name']}: {reason}")
print(f"  Output:    {len(gdf_clean)} candidates for scoring + manual review")
print(f"\nNext steps:")
print(f"  1. Open outputs/csv/verification_links_{STATE}.csv")
print(f"  2. Click each google_satellite link — 1-2 min per site")
print(f"  3. Mark status column (CONFIRMED_VACANT / ACTIVE / NEEDS_MORE_INFO)")
print(f"  4. python3 score_and_export.py")
