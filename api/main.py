"""Tributary Toolkit HTTP API.

Bind to loopback only:
    uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

The Vite dev server (web/) proxies /api to this process.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .flowlines import fetch_flowlines
from .errors import http_error
from .sources import router as sources_router
from .streamstats import router as streamstats_router

app = FastAPI(title="Tributary Toolkit")

# Vite defaults. The page and the API are different ports
# during development, so the browser needs this permission.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Point-Count",
        "X-Series-Count",
        "X-Partial-Errors",
        "Content-Disposition",
    ],
)

app.include_router(streamstats_router, prefix="/api/streamstats")
app.include_router(sources_router, prefix="/api/sources")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/flowlines")
def flowlines(
    west: float,
    south: float,
    east: float,
    north: float,
) -> dict:
    # No verify_ssl here on purpose. Stream lines always skip the check.
    try:
        return fetch_flowlines(west, south, east, north)
    except Exception as exc:
        raise http_error(exc) from exc
