"""Local HTTP API for the Tributary Toolkit.

Thin door in front of streamstats/ and data_sources/.
The browser holds the map; these routes only run when a
button asks for data.

Run from the data-downloader folder:
    uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
"""

import sys
from pathlib import Path

# uvicorn api.main:app — keep the repo root importable so
# `import data_sources` / `import streamstats` work.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
