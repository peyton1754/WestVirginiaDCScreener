# West Virginia Data Center Screener

Automated screening pipeline that identifies retired industrial brownfield sites in West Virginia suitable for behind-the-meter (BTM) data center development. Forked from the Tennessee pipeline with West Virginia-specific data sources, a statewide parcel GIS endpoint, and Appalachian Power/Mon Power market context.

The target use case is 100+ MW hyperscale data center campuses with on-site gas-fired generation, where the ideal site is a large (25+ acre) retired industrial parcel with gas pipeline access, high-voltage grid interconnection, and an established air permit framework. Sites with confirmed parcel size below 25 acres are hard-excluded; sites between 25–50 acres are flagged for adjacent land availability analysis.

## Why West Virginia

- **2025 behind-the-meter law** — West Virginia amended its high-impact-industrial-facility statute in 2025 to explicitly cover "high impact data centers," authorizing them to draw power from behind-the-meter generators. This is a direct, recent policy tailwind for exactly this pipeline's use case, distinct from (and more specific than) the general industrial-rate discounts other states in this family rely on.
- **Industrial power costs** — Appalachian Power (AEP) serves southern/central WV, Mon Power and Potomac Edison (FirstEnergy) serve the north; average industrial rates run ~6.3 cents/kWh, well below the national average. AEP also offers negotiated rate reductions (~15%) for large new industrial loads (500kW+, 10+ jobs, $2.5M+ investment).
- **Industrial brownfield density** — Steel (Weirton, Wheeling, Mingo Junction in the Northern Panhandle), heavy chemicals (Kanawha Valley/"Chemical Valley" around Charleston, South Charleston, Institute), and a long legacy of coal-adjacent industrial sites statewide.
- **Pipeline coverage** — Mountain Valley Pipeline (42", WV/VA, began operations 2024), Columbia Gas Transmission, Equitrans Midstream, and Texas Eastern Transmission all run through the state — dense Marcellus/Utica-adjacent gas infrastructure.

## Pipeline Architecture

```
┌─ DATA COLLECTION ──────────────────────────────────────────────────────────┐
│                                                                            │
│  download_data.py            National / multi-state datasets               │
│    ├─ EPA FRS                 WV facility registry                        │
│    ├─ EIA Form 860            Retired generators                          │
│    ├─ HIFLD Substations       In-service ≥115kV (DOE/ORNL)                │
│    └─ HIFLD Transmission      ≥230kV lines (DOE/ORNL)                     │
│                                                                            │
│  download_gas_pipelines_wv.py EIA interstate/intrastate gas pipelines      │
│    └─ WV region (~2,600 segments), incl. Operator + pipe type              │
│                                                                            │
│  download_westvirginia.py     West Virginia-specific datasets              │
│    ├─ EIA Form 861            AEP/Mon Power industrial rates (WV)          │
│    ├─ Census TIGER            County boundaries, tracts, census blocks     │
│    ├─ FCC Form 477            Fiber presence at census block (WV, FIPS 54) │
│    ├─ EPA ACRES/SEMS          Brownfield registry + Superfund sites        │
│    └─ BLS LAUS                County employment / labor market size        │
│                                                                            │
│  download_westvirginia_extras.py Hazard + environment datasets             │
│    ├─ USFWS NWI Wetlands      WV geodatabase                               │
│    ├─ USGS PAD-US 4.1         Protected areas (WV)                        │
│    ├─ NOAA Storm Events       WV events 2015-2025 (tornadoes, wind)        │
│    └─ FEMA NRI                County-level composite hazard scores (WV)    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
┌─ CANDIDATE BUILDING ──────────────┴────────────────────────────────────────┐
│                                                                            │
│  build_candidates.py         Six sources                                   │
│    ├─ EIA 860 retired        Coal, gas, oil power plants ≥50 MW            │
│    ├─ EPA FRS                Two-pathway gate: ACRES/SEMS registry, or     │
│    │                         2+ FRS program signals (TRI + air permit)     │
│    ├─ EPA TRI                Closed facilities in target NAICS sectors     │
│    ├─ OSM industrial landuse Catch-all for unmapped brownfields            │
│    ├─ EPA Redevelopment      Brownfields >100 acres, no reported           │
│    │  Mapper                 redevelopment (curated ACRES subset —         │
│    │                         see EPA-540-S-26-001, Jan 2026)               │
│    ├─ NAICS filter           Wood, paper, petroleum, chemical, minerals,   │
│    │                         steel (NAICS 321/322/324/325/327/331)         │
│    └─ Active company blocklist  Removes known operating companies          │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
┌─ SPATIAL FILTERING ───────────────┴────────────────────────────────────────┐
│                                                                            │
│  filter_pipeline.py                                                        │
│                                                                            │
│  Hard filters (site must pass all):                                        │
│    [a] Within 5 mi of 230kV+ transmission line                            │
│    [b] Within 60 mi of large urban area (≥250k pop)                       │
│    [c] Within 3 mi of 115kV+ substation                                   │
│    [d] Within 10 mi of EIA gas pipeline                                   │
│    [e] Outside EPA ozone/PM2.5 nonattainment                              │
│    [f] Outside FEMA SFHA flood zones (live API)                           │
│    [i] Within 10 mi of FCC fiber census block                             │
│    [j] Outside 0.5 mi of EPA Superfund/SEMS site                         │
│    [m] Outside PAD-US GAP 1-2 protected areas                             │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
┌─ ENRICHMENT ──────────────────────┴────────────────────────────────────────┐
│                                                                            │
│  fetch_epa_compliance.py     Live EPA ECHO/ICIS-AIR/ICIS-NPDES query       │
│    └─ Per-site, by FRS registry ID — see Data Sources below                │
│                                                                            │
│  enrich_retirement.py        Retirement confidence scoring (v3)            │
│    ├─ EPA TRI closed flag                                                  │
│    ├─ ICIS-AIR permit status   Permanently Closed / operating status       │
│    ├─ ICIS-NPDES terminations  All water permits terminated                │
│    ├─ EPA ECHO inspection      Days since last inspection                  │
│    ├─ WARN Act matching        WorkForce West Virginia notices (PDF-only,  │
│    │                           manual-only source — see Data Sources)      │
│    └─ Active operator blocklist                                            │
│                                                                            │
│  enrich_columns.py           Gap-fill from statewide GIS + federal APIs   │
│    ├─ WV statewide parcels     Owner + acreage, all 55 counties in one     │
│    │                           composite service (no county overrides     │
│    │                           needed, unlike Tennessee)                   │
│    ├─ USGS WBD                 HUC8 watershed basin per site               │
│    └─ USDA SSURGO               Soil drainage and hydric rating            │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
┌─ SCORING & EXPORT ────────────────┴────────────────────────────────────────┐
│                                                                            │
│  score_and_export.py                                                       │
│    ├─ Hard exclude: active permits / recent inspections                    │
│    ├─ Hard exclude: confirmed parcel < 25 acres                            │
│    ├─ Infrastructure score     10 dimensions, 120 pts max                  │
│    ├─ Retirement multiplier    VERY_HIGH×1.0 → ACTIVE_WARNING×0.5         │
│    ├─ WV state score           11/18 pts (AEP/Mon Power rate + 2025 BTM    │
│    │                           law tailwind, smaller current DC market)    │
│    ├─ USGS seismic             ASCE 7-22 design parameters (live API)     │
│    ├─ USDA SSURGO               Soil type, drainage, hydric rating          │
│    ├─ NOAA storms              Tornado + severe wind history by county     │
│    ├─ FEMA NRI                 18-hazard composite risk score              │
│    └─ Watershed                HUC8 basin from USGS WBD service            │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

## Key West Virginia Markets

| Metro | Key Assets | Notable Brownfield Types |
|-------|-----------|--------------------------|
| **Charleston (Kanawha)** | State capital, "Chemical Valley" corridor (Institute, South Charleston) | Heavy chemicals, plastics |
| **Weirton/Wheeling (Hancock/Brooke/Ohio)** | Northern Panhandle steel corridor, dense PJM transmission, close to PA/OH grid | Steel, metals fabrication |
| **Huntington (Cabell)** | Ohio River logistics, tri-state (WV/OH/KY) market | Chemicals, glass, metals |
| **Morgantown (Monongalia)** | WVU research park, I-79 corridor, growing tech presence | Coal-adjacent industrial, chemicals |
| **Parkersburg (Wood)** | Mid-Ohio Valley chemical corridor | Chemicals, plastics |

## County GIS Parcel Coverage

Unlike Tennessee (whose statewide parcel layer only covers ~90 of 95 counties, with several metro counties needing their own fallback endpoints), West Virginia has a single statewide composite parcel service covering all 55 counties — no county-by-county overrides are needed:

| Coverage | GIS Source |
|----------|------------|
| All 55 counties — owner + acreage | services.wvgis.wvu.edu (WVGIS, `Planning_Cadastre/WV_Parcels`) |

Note: this service accepts envelope geometry queries only — point+distance queries return a 400 error, and requesting a `Shape__Area` outField (present on some other Esri services in this pipeline family) also errors the whole request. `fetch_parcels_wv.py`, `enrich_columns.py`, and `check_adjacent_land.py` all query it with a small lon/lat envelope for this reason. Re-verify this endpoint with a live query before relying on it if `current_owner` fill rates drop unexpectedly — GIS providers periodically move or rename services.

## Setup

**Prerequisites:** Python 3.10+ and pip. No API keys or accounts are required — every data source below is free and publicly accessible.

```bash
git clone https://github.com/Arthurfok1/WestVirginiaDCScreener.git
cd WestVirginiaDCScreener
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

`geopandas` pulls in GDAL/GEOS/PROJ binaries via `pyogrio` — on Linux you may need `apt install libgdal-dev` (or your distro's equivalent) first if the wheel install fails; macOS and Windows wheels bundle these already.

**Disk space & runtime:** the download stage pulls ~1–2 GB (national HIFLD transmission lines, NHD water bodies, NWI wetlands, Census TIGER blocks are the largest files) and takes roughly 15–30 minutes depending on connection speed — most scripts skip re-downloading files that already exist, so re-runs after the first are much faster. The rest of the pipeline (candidate building through scoring) typically finishes in a few minutes.

## Running the Pipeline

Run each stage in order — later stages read the previous stage's output from `data/processed/` and `data/raw/`:

```bash
python3 download_data.py                # National datasets
python3 download_westvirginia.py        # WV-specific datasets
python3 download_westvirginia_extras.py # Hazard / environment datasets
python3 download_gas_pipelines_wv.py    # EIA gas pipeline geometries (WV region)
python3 build_candidates.py             # Build candidate pool
python3 filter_pipeline.py              # Apply spatial filters
python3 fetch_epa_compliance.py         # Live EPA ECHO/ICIS-AIR/ICIS-NPDES query (needs site_id)
python3 enrich_retirement.py            # Score retirement confidence
python3 enrich_columns.py               # Fill ownership, parcels, soil
python3 score_and_export.py             # Score and export rankings
python3 check_adjacent_land.py          # Adjacent land for 25-50 ac sites
```

**Output:** the ranked candidate list lands at `outputs/csv/top_candidates_wv.csv` (and `.geojson` for mapping), with `outputs/csv/adjacent_land_25_50ac.csv` covering expansion potential for mid-size sites. Intermediate data lives in `data/raw/` (downloaded source files) and `data/processed/` (candidate pool at each pipeline stage) if you want to inspect or debug a specific step.

## Data Sources

| Dataset | Source | Notes |
|---------|--------|-------|
| EPA FRS | ftp.epa.gov/frs/ | West Virginia facility registry |
| EPA ECHO / ICIS-AIR / ICIS-NPDES | echodata.epa.gov/echo (echo_rest_services, air_rest_services, cwa_rest_services) | Live per-site query by FRS registry ID via `fetch_epa_compliance.py`, not a bulk download — see that script's header comment for the verified API behavior (one registry ID per request, no comma-separated batching; two-step get_facilities→get_qid flow) |
| EPA Redevelopment Mapper | services.arcgis.com/cJ9YHowT8TU7DUyn (`Brownfield_Properties_Over_100_Acres_view`) | ACRES subset EPA curated for Superfund/Brownfield-to-data-center reuse; see `build_candidates.py`'s Source 5 |
| EIA Form 860 | eia.gov/electricity/data/eia860/ | Retired generators |
| HIFLD Substations | services6.arcgis.com/OO2s4OoyCZkYJ6oE | DOE/ORNL in-service ≥115kV |
| EIA Gas Pipelines | services2.arcgis.com/FiaPA4ga0iQKduv3 (Natural_Gas_Interstate_and_Intrastate_Pipelines_1) | Interstate + intrastate, fetched via `download_gas_pipelines_wv.py` |
| WV Statewide Parcels | services.wvgis.wvu.edu (Planning_Cadastre/WV_Parcels) | Owner + acreage, all 55 counties in one composite service |
| USGS NSHM | earthquake.usgs.gov/ws/designmaps/asce7-22.json | Seismic design parameters |
| USGS WBD | hydro.nationalmap.gov/arcgis | HUC8 watershed boundaries |
| USDA SSURGO | SDMDataAccess.sc.egov.usda.gov | Soil drainage and hydric ratings |
| FEMA NRI | services.arcgis.com/XG15cJAlne2vxtgt | County hazard risk index |
| NOAA Storm Events | ncei.noaa.gov/pub/data/swdi/stormevents/ | Tornado and wind history |
| USFWS NWI | documentst.ecosphere.fws.gov/wetlands/ | West Virginia wetlands geodatabase |
| USGS PAD-US 4.1 | sciencebase.gov | Protected areas (WV) |
| FCC Form 477 | fcc.gov | Fiber census blocks (WV, FIPS 54) |
| Census TIGER 2023 | census.gov | County boundaries, urban areas, blocks |

**Manual-only (no free bulk endpoint — see `download_westvirginia_extras.py`'s `print_manual_sources()`):**

| Source | Why manual |
|--------|-----------|
| WorkForce WV WARN notices | Published as a periodically-updated PDF listing, not a CSV/API feed |
| WV SOS business entities | Search-only system, no bulk download |
| WVDEP water withdrawal / large-quantity-user data | ESS + interactive tool only, no bulk export |
| WVDEP Voluntary Remediation Program site list | No public GIS layer or bulk-downloadable list; EPA ACRES/SEMS/RCRA used for corroboration instead |
