"""
Fetch EIA interstate/intrastate natural gas pipeline geometries for the
West Virginia region and save to data/raw/hifld/eia_gas_pipelines.geojson,
the file filter_pipeline.py's step [d] expects.

Source: EIA Natural Gas Interstate and Intrastate Pipelines feature service
(the same national service used by every state in this pipeline family —
only the bounding box below is state-specific).
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
# West Virginia bbox (-82.7,37.2,-77.7,40.6) padded ~40mi so the 10-mile
# filter has margin at state edges
BBOX = "-83.4,36.5,-76.9,41.3"

RAW_DIR = Path("data/raw/hifld")
RAW_DIR.mkdir(parents=True, exist_ok=True)
DEST = RAW_DIR / "eia_gas_pipelines.geojson"

if DEST.exists():
    print(f"[skip] {DEST} already exists")
else:
    print("Fetching EIA gas pipelines for West Virginia region...")
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
