"""
Fetch EIA interstate/intrastate natural gas pipeline geometries for the
Tennessee region and save to data/raw/hifld/eia_gas_pipelines.geojson,
the file filter_pipeline.py's step [d] expects.

Source: EIA Natural Gas Interstate and Intrastate Pipelines feature service
(the old services2.arcgis.com/FiaPA4ga0iQKduv3 org referenced in the README
is still live for this dataset — it's just the Shelby County parcel service
under that same org that has been decommissioned).
"""
import json
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

URL = (
    "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
    "Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0/query"
)
# Tennessee bbox padded ~60mi so the 10-mile filter has margin at state edges
BBOX = "-91.0,34.5,-81.0,37.0"

RAW_DIR = Path("data/raw/hifld")
RAW_DIR.mkdir(parents=True, exist_ok=True)
DEST = RAW_DIR / "eia_gas_pipelines.geojson"

if DEST.exists():
    print(f"[skip] {DEST} already exists")
else:
    print("Fetching EIA gas pipelines for Tennessee region...")
    r = requests.get(URL, params={
        "geometry": BBOX,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "TYPEPIPE,Operator,Status",
        "outSR": "4326",
        "returnGeometry": "true",
        "f": "geojson",
    }, timeout=60, verify=False, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    geojson = r.json()
    n = len(geojson.get("features", []))
    with open(DEST, "w") as fh:
        json.dump(geojson, fh)
    print(f"Saved {n:,} pipeline segments -> {DEST}")
