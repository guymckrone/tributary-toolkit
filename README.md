# Tributary Toolkit

Stream restoration toolkit. Today that means a **Data Download** tool
(USGS, Portland BES Aquarius, NRCS SNOTEL) plus a **StreamStats**
shell for the next watershed-batch feature.

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
| **StreamStats** | Single point: snap + delineate, basin map, GeoJSON download |

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

## Layout

```
data-downloader/
  app.py                 # Toolkit home (streamlit run app.py)
  pages/                 # Data Download, StreamStats, …
  data_sources/          # BaseDataSource + USGS / Aquarius / SNOTEL + registry
  streamstats/           # StreamStats SS-Delineate client + smoke
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
