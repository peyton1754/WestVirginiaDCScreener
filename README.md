# Tennessee Data Center Screener

Automated screening pipeline that identifies retired industrial brownfield sites in Tennessee suitable for behind-the-meter (BTM) data center development. Forked from the Alabama pipeline with Tennessee-specific data sources, county GIS endpoints, and TVA power market context.

The target use case is 100+ MW hyperscale data center campuses with on-site gas-fired generation, where the ideal site is a large (25+ acre) retired industrial parcel with gas pipeline access, high-voltage grid interconnection, and an established air permit framework. Sites with confirmed parcel size below 25 acres are hard-excluded; sites between 25–50 acres are flagged for adjacent land availability analysis.

## Why Tennessee

- **TVA power rates** — Tennessee Valley Authority industrial rates (~4–5 cents/kWh) are among the lowest in the US. TVA operates a formal Large Power Interconnection program for loads >5 MW.
- **Industrial brownfield density** — Heavy chemical corridor (Eastman Chemical in Kingsport), steel and foundries in Chattanooga, paper/pulp mills in East Tennessee, auto manufacturing brownfields (Saturn/GM Spring Hill, Smyrna corridor).
- **Growing DC market** — Nashville is a fast-growing secondary market; Oracle, Switch, and Microsoft have recent presence. Chattanooga's EPB municipal gigabit fiber is nationally unique for a secondary market.
- **Pipeline coverage** — Tennessee Gas Pipeline (Kinder Morgan) and Columbia Gulf run through the state; Chattanooga and Kingsport have dense industrial-grade gas access.

## Pipeline Architecture

```
┌─ DATA COLLECTION ──────────────────────────────────────────────────────────┐
│                                                                            │
│  download_data.py            National / multi-state datasets               │
│    ├─ EPA FRS                 TN facility registry                         │
│    ├─ EIA Form 860            Retired generators                           │
│    ├─ HIFLD Substations       In-service ≥115kV (DOE/ORNL)                │
│    └─ HIFLD Transmission      ≥230kV lines (DOE/ORNL)                     │
│                                                                            │
│  download_gas_pipelines_tn.py EIA interstate/intrastate gas pipelines      │
│    └─ TN region (~500 segments), incl. Operator + pipe type                │
│                                                                            │
│  download_tennessee.py        Tennessee-specific datasets                  │
│    ├─ EIA Form 861            TVA + co-op utility industrial rates (TN)    │
│    ├─ Census TIGER            County boundaries, tracts, census blocks     │
│    ├─ FCC Form 477            Fiber presence at census block (TN, FIPS 47) │
│    ├─ EPA ACRES/SEMS          Brownfield registry + Superfund sites        │
│    └─ BLS LAUS                County employment / labor market size        │
│                                                                            │
│  download_tennessee_extras.py Hazard + environment datasets                │
│    ├─ USFWS NWI Wetlands      TN geodatabase                               │
│    ├─ USGS PAD-US 4.1         Protected areas (TN)                         │
│    ├─ NOAA Storm Events       TN events 2015-2025 (tornadoes, wind)        │
│    └─ FEMA NRI                County-level composite hazard scores (TN)    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
┌─ CANDIDATE BUILDING ──────────────┴────────────────────────────────────────┐
│                                                                            │
│  build_candidates.py         Two-pathway entry gate                        │
│    ├─ Pathway A: EPA ACRES/SEMS registry (confirmed brownfields)           │
│    └─ Pathway B: 2+ FRS program signals (TRI + air permit, etc.)          │
│    ├─ NAICS filter           Wood, paper, petroleum, chemical, minerals,   │
│    │                         steel (NAICS 321/322/324/325/327/331)         │
│    ├─ EIA 860 retired        Coal, gas, oil power plants ≥50 MW            │
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
│    ├─ WARN Act matching        Tennessee Dept of Labor notices             │
│    └─ Active operator blocklist                                            │
│                                                                            │
│  enrich_columns.py           Gap-fill from county GIS + federal APIs      │
│    ├─ TN statewide parcels     Owner + acreage, ~90 rural counties         │
│    ├─ County GIS overrides     Davidson, Rutherford (owner+acres),         │
│    │                           Shelby (acres only — no live owner API)     │
│    ├─ USGS WBD                 HUC8 watershed basin per site               │
│    └─ USDA SSURGO              Soil drainage and hydric rating             │
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
│    ├─ TVA state score          14/18 pts (TVA rate advantage)              │
│    ├─ USGS seismic             ASCE 7-22 design parameters (live API)     │
│    ├─ USDA SSURGO              Soil type, drainage, hydric rating          │
│    ├─ NOAA storms              Tornado + severe wind history by county     │
│    ├─ FEMA NRI                 18-hazard composite risk score              │
│    └─ Watershed                HUC8 basin from USGS WBD service            │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

## Key Tennessee Markets

| Metro | Key Assets | Notable Brownfield Types |
|-------|-----------|--------------------------|
| **Nashville (Davidson)** | Fast-growing DC market, Oracle/Switch presence | Auto, printing, chemicals |
| **Chattanooga (Hamilton)** | EPB gigabit fiber, TVA HQ nearby, 230kV grid | Steel, foundries, textiles |
| **Knoxville (Knox)** | TVA nuclear corridor, UT research park | Textiles, chemicals, paper |
| **Kingsport (Sullivan)** | Eastman Chemical corridor, dense gas pipeline access | Heavy chemicals, coal coke |
| **Memphis (Shelby)** | Major logistics hub, Mississippi River cooling | Paper, metals, petroleum |
| **Murfreesboro (Rutherford)** | Former Nissan/GM auto corridor | Auto manufacturing |

## County GIS Parcel Coverage

Owner and acreage lookup uses the TN Comptroller's statewide parcel layer first
(`Tennessee_Property_Boundaries_Public_Use`, ~90 of 95 counties — mostly rural
counties on the state's own assessment system), then falls back to per-county
overrides for the metro counties that run their own GIS instead:

| County | Metro | Coverage | GIS Source |
|--------|-------|----------|------------|
| *(most counties)* | — | Owner + acreage | services1.arcgis.com (statewide, YuVBSS7Y1of2Qud1) |
| Davidson | Nashville | Owner + acreage | maps.nashville.gov/arcgis (Cadastral/Parcels) |
| Rutherford | Murfreesboro | Owner + acreage | services5.arcgis.com (A5C0MR9xfkxVRwat) |
| Shelby | Memphis | Acreage only | 311.memphistn.gov (Shelby's own Assessor API errors server-side) |
| Knox | Knoxville | Unavailable | KGIS requires an authenticated login — no public REST endpoint |
| Hamilton | Chattanooga | Unavailable | No public REST parcel endpoint (interactive viewers only) |

County GIS providers periodically move, rename, or lock down their services —
re-verify these endpoints with a live query before relying on them if
`current_owner` fill rates drop unexpectedly.

## Setup

**Prerequisites:** Python 3.10+ and pip. No API keys or accounts are required — every data source below is free and publicly accessible.

```bash
git clone https://github.com/Arthurfok1/TennesseeDCScreener.git
cd TennesseeDCScreener
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

`geopandas` pulls in GDAL/GEOS/PROJ binaries via `pyogrio` — on Linux you may need `apt install libgdal-dev` (or your distro's equivalent) first if the wheel install fails; macOS and Windows wheels bundle these already.

**Disk space & runtime:** the download stage pulls ~1–2 GB (national HIFLD transmission lines, NHD water bodies, NWI wetlands, Census TIGER blocks are the largest files) and takes roughly 15–30 minutes depending on connection speed — most scripts skip re-downloading files that already exist, so re-runs after the first are much faster. The rest of the pipeline (candidate building through scoring) typically finishes in a few minutes.

## Running the Pipeline

Run each stage in order — later stages read the previous stage's output from `data/processed/` and `data/raw/`:

```bash
python3 download_data.py              # National datasets
python3 download_tennessee.py         # TN-specific datasets
python3 download_tennessee_extras.py  # Hazard / environment datasets
python3 download_gas_pipelines_tn.py  # EIA gas pipeline geometries (TN region)
python3 build_candidates.py           # Build candidate pool
python3 filter_pipeline.py            # Apply spatial filters
python3 fetch_epa_compliance.py       # Live EPA ECHO/ICIS-AIR/ICIS-NPDES query (needs site_id)
python3 enrich_retirement.py          # Score retirement confidence
python3 enrich_columns.py             # Fill ownership, parcels, soil
python3 score_and_export.py           # Score and export rankings
python3 check_adjacent_land.py        # Adjacent land for 25-50 ac sites
```

**Output:** the ranked candidate list lands at `outputs/csv/top_candidates_tn.csv` (and `.geojson` for mapping), with `outputs/csv/adjacent_land_25_50ac.csv` covering expansion potential for mid-size sites. Intermediate data lives in `data/raw/` (downloaded source files) and `data/processed/` (candidate pool at each pipeline stage) if you want to inspect or debug a specific step.

## Data Sources

| Dataset | Source | Notes |
|---------|--------|-------|
| EPA FRS | ftp.epa.gov/frs/ | Tennessee facility registry |
| EPA ECHO / ICIS-AIR / ICIS-NPDES | echodata.epa.gov/echo (echo_rest_services, air_rest_services, cwa_rest_services) | Live per-site query by FRS registry ID via `fetch_epa_compliance.py`, not a bulk download — see that script's header comment for the verified API behavior (one registry ID per request, no comma-separated batching; two-step get_facilities→get_qid flow) |
| EIA Form 860 | eia.gov/electricity/data/eia860/ | Retired generators |
| HIFLD Substations | services6.arcgis.com/OO2s4OoyCZkYJ6oE | DOE/ORNL in-service ≥115kV |
| EIA Gas Pipelines | services2.arcgis.com/FiaPA4ga0iQKduv3 (Natural_Gas_Interstate_and_Intrastate_Pipelines_1) | Interstate + intrastate, fetched via `download_gas_pipelines_tn.py` |
| TN Statewide Parcels | services1.arcgis.com/YuVBSS7Y1of2Qud1 (Tennessee_Property_Boundaries_Public_Use) | Owner + acreage, ~90 counties |
| USGS NSHM | earthquake.usgs.gov/ws/designmaps/asce7-22.json | Seismic design parameters |
| USGS WBD | hydro.nationalmap.gov/arcgis | HUC8 watershed boundaries |
| USDA SSURGO | SDMDataAccess.sc.egov.usda.gov | Soil drainage and hydric ratings |
| FEMA NRI | services.arcgis.com/XG15cJAlne2vxtgt | County hazard risk index |
| NOAA Storm Events | ncei.noaa.gov/pub/data/swdi/stormevents/ | Tornado and wind history |
| USFWS NWI | documentst.ecosphere.fws.gov/wetlands/ | Tennessee wetlands geodatabase |
| USGS PAD-US 4.1 | sciencebase.gov | Protected areas (TN) |
| FCC Form 477 | fcc.gov | Fiber census blocks (TN, FIPS 47) |
| Census TIGER 2023 | census.gov | County boundaries, urban areas, blocks |
