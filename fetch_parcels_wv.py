"""
Fetch West Virginia parcel acreage from the WVGIS statewide parcel layer.
Writes parcel_acres back into the GeoPackages used by score_and_export.py.

Source: West Virginia GIS Technical Center (WVGISTC) / WV State Tax and
  Revenue Authority statewide parcel composite
  https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels/MapServer/0

Unlike Tennessee/Alabama, West Virginia has a single statewide composite
service — no county-by-county fallback needed.
"""
import math, requests, pandas as pd, geopandas as gpd, urllib3, time
from pathlib import Path

urllib3.disable_warnings()

URL = (
    "https://services.wvgis.wvu.edu/arcgis/rest/services/"
    "Planning_Cadastre/WV_Parcels/MapServer/0/query"
)
H = {"User-Agent": "Mozilla/5.0"}

def to_webmercator(lon, lat):
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    y = y * 20037508.34 / 180
    return x, y

def query_parcel(lon, lat, pad=400):
    x, y = to_webmercator(lon, lat)
    for attempt_pad in [pad, 800, 1500]:
        try:
            r = requests.get(URL, params={
                "geometry": f"{x-attempt_pad},{y-attempt_pad},{x+attempt_pad},{y+attempt_pad}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "102100",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "FullOwnerName,Acres_C,CountyID,FullPhysicalAddress",
                "returnGeometry": "false",
                "f": "json"
            }, timeout=20, verify=False, headers=H)
            data = r.json()
            feats = data.get("features", [])
            if feats:
                best = max(feats, key=lambda f: f["attributes"].get("Acres_C") or 0)
                attrs = best["attributes"]
                return attrs.get("Acres_C"), attrs.get("FullOwnerName", "")
        except Exception as e:
            print(f"  ERROR: {e}")
            return None, None
    return None, None

PROC_DIR = Path("data/processed")
gpkg_paths = [
    PROC_DIR / "candidates_enriched_wv.gpkg",
    PROC_DIR / "candidates_filtered_wv.gpkg",
]

# Use the enriched GeoPackage as the source of truth for coordinates
src_path = PROC_DIR / "candidates_enriched_wv.gpkg"
gdf = gpd.read_file(src_path).to_crs("EPSG:4326")

# Build a lookup: plant_id / site_id → (acres, owner)
lookup = {}  # keyed by integer index position
hits = 0

for i, row in gdf.iterrows():
    geom = row.geometry
    if geom is None:
        name = row.get("Plant_Name", f"row {i}")
        print(f"[{i+1:3d}] {name:<45} SKIP (no geometry)")
        continue
    lon, lat = geom.x, geom.y
    name = row.get("Plant_Name", f"row {i}")
    acres, owner = query_parcel(float(lon), float(lat))
    if acres:
        lookup[i] = (round(float(acres), 2), owner or "")
        print(f"[{i+1:3d}] {name:<45} {acres:.1f} ac  | {owner}")
        hits += 1
    else:
        lookup[i] = (None, None)
        print(f"[{i+1:3d}] {name:<45} no parcel data")
    time.sleep(0.1)

print(f"\nFetched {hits}/{len(gdf)} parcels. Writing to GeoPackages…")

# Write results into each GeoPackage
for gpkg_path in gpkg_paths:
    g = gpd.read_file(gpkg_path)
    if "parcel_acres" not in g.columns:
        g["parcel_acres"] = None
    if "parcel_owner" not in g.columns:
        g["parcel_owner"] = None

    # Match by Plant_Name since indices may differ between filtered/enriched
    acres_map = {gdf.loc[i, "Plant_Name"]: v for i, v in lookup.items()}
    for j, row in g.iterrows():
        name = row.get("Plant_Name")
        if name in acres_map:
            ac, own = acres_map[name]
            g.at[j, "parcel_acres"] = ac
            g.at[j, "parcel_owner"] = own

    g.to_file(gpkg_path, driver="GPKG")
    enriched = g["parcel_acres"].notna().sum()
    print(f"  {gpkg_path.name}: {enriched}/{len(g)} sites with acreage")

print("\nDone. Re-run score_and_export.py to update rankings.")
