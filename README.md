# Tributary Toolkit

Stream restoration toolkit. Today that means a **Data Download** tool
(USGS, Portland BES Aquarius, NRCS SNOTEL) and a **StreamStats** page
(snap, delineate, basin characteristics, flow statistics, and
watershed downloads). The page you use day to day is the local web
app below. Streamlit still opens, and it is the older shell.

## Requirements

- Windows + PowerShell / cmd (use `;` between commands in PowerShell, not `&&`)
- Python / conda env with `requests`, `pandas`, `python-dotenv`
- A `.env` file in this folder (never commit it):

```env
AQUARIUS_BASE_URL=...
AQUARIUS_USERNAME=...
AQUARIUS_PASSWORD=...
USGS_API_KEY=...          # optional; ~1000 req/hr with key vs ~50 without
OUTPUT_DIRECTORY=...
```

## Backend (library + live tests)

```powershell
cd data-downloader
python tests\hammer_test.py          # all three sources (~30 s)
python tests\hammer_test.py usgs     # usgs | aquarius | snotel
```

Example (Python):

```python
from data_sources import create_source, list_sources

for desc in list_sources():
    print(desc.key, desc.display_name)

usgs = create_source("usgs")
sites = usgs.get_sites(state="OR", site_ids=["14211400"])
params = usgs.available_parameters_dataframe(["14211400"])
```

## Streamlit GUI (toolkit hub)

Install GUI extras into the **same** env you use to run Streamlit
(if Aquarius fails with `No module named 'dotenv'`, you're probably on
base miniforge — install the requirements there or activate your project env):

```powershell
cd data-downloader
pip install -r requirements-gui.txt
streamlit run app.py
```

Browser opens at `http://localhost:8501`. Sidebar pages:

| Page | Status |
|---|---|
| **Home** (`app.py`) | Toolkit overview |
| **Data Download** | Full gage/time-series GUI |
| **StreamStats** | Older single-point page. The current one is the web UI |

### Data Download workflow

1. Open **Data Download**; pick a source in the sidebar (USGS / Aquarius / SNOTEL).
2. **Find sites** — state / site IDs / USGS agency+type, **or**
   draw a box, **or** upload a shapefile ZIP.
3. Narrow with **Include / Exclude / Search** (no extra API calls).
4. Click map pins or use **Sites to inspect** → **Show available parameters**.
5. Under **Download**, pick series + dates → **Download data**.
6. Save as **one CSV/SQLite** or **one file per series (ZIP)**.

### Quick test sites

| Source | Example |
|---|---|
| USGS | `14211400` (Johnson Creek) — use Agency `USGS`, Site type `ST` |
| Aquarius | Find sites, Include `HYDRA`, or id `HYDRA-1` |
| SNOTEL | State `OR`, or `302:OR:SNTL` — turn off Verify SSL on the work network |

### Notes

- USGS Oregon mixes other agencies (`OR004-…`); many have no downloadable
  series in the continuous/daily API. Prefer Agency `USGS`.
- Shapefile search uses the file's **bounding box**, not the exact
  polygon. Lon/lat (WGS84) only; projected shapefiles are rejected.
- SNOTEL map-area / shapefile search still needs a **State** filled in.
- Work network: leave Aquarius SSL verification off; turn SNOTEL SSL off
  in the sidebar if requests fail.

## Web UI (map in the browser)

This is the UI to use. The page is `web/`; the API is `api/`. Both
processes have to be running. Python stays in the `datadownload`
conda env. Node stays in `toolkit-web`. Do not install Node into
`datadownload` (that once broke NumPy).

```powershell
cd data-downloader
conda activate datadownload
pip install -r requirements-api.txt
python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

Second terminal:

```powershell
cd data-downloader\web
conda activate toolkit-web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The page proxies `/api` to port 8000.
The API binds to loopback only. `npm run build` writes `web/dist/`
for a later packaged copy; day-to-day work uses `npm run dev`.

| Hash | Page |
|---|---|
| `#streamstats` | Snap and delineate, one point or several, hydro tiles, basin characteristics, flow statistics, downloads |
| `#download` | Find sites, parameters, CSV / SQLite download |

### StreamStats on that page

1. Pick a state (Oregon is the one the smoke check uses).
2. Click the map, or switch to several points and drag the markers.
   Picked points are snapped onto the stream before the basin is drawn.
3. Optional: basin characteristics, then flow statistics. Flow
   statistics need the characteristics, and checking that box turns
   characteristics on.
4. Download GeoJSON, a shapefile zip, KML, or KMZ. Several basins
   come back as one zip of separate files. Characteristics and flow
   statistics, when you asked for them, are CSV files in that zip.

Hydrography on the map is a tiled USGS basemap. It is a guide for
aiming. The basin itself comes from StreamStats.

### Verify SSL

The checkbox is per page.

- **StreamStats** (snap, delineate, characteristics, flow statistics):
  leave it checked. That USGS host verified on the work network
  (Oct 30, 2026). The hydro tiles are loaded by the browser and do
  not use this checkbox.
- **Data Download:** Aquarius and SNOTEL often fail certificate
  checks on the work network. Uncheck Verify SSL on that page if
  those requests fail. USGS Water Data usually keeps it on.

A production copy of the page, from `web/` with `toolkit-web` active:

```powershell
npm run build
```

StreamStats live check, from `data-downloader` with `datadownload` active:

```powershell
python -m streamstats.smoke
```

## Layout

```
data-downloader/
  app.py                 # Toolkit home (streamlit run app.py)
  api/                   # FastAPI routes for the web UI
  web/                   # Browser UI (Vite). npm run dev
  pages/                 # Older Streamlit pages
  data_sources/          # BaseDataSource + USGS / Aquarius / SNOTEL + registry
  streamstats/           # SS-Delineate, SS-Hydro, NSS, and the live smoke check
  gui/                   # Data Download Streamlit modules
  analysis/              # Storm / gauge analysis (precip SQLite → storms)
  tests/                 # Live hammer_test.py
  aquarius_data_downloader.py
  config.py
  requirements-gui.txt
```

Storm / gauge scripts (from this folder):

```powershell
python -m analysis.regional_storm_analysis
python -m analysis.gauge_analysis
```
