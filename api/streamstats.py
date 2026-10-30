"""Snap / watershed routes.

Each call builds a fresh StreamStatsClient. No session on
the server — the browser sends the point every time.
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from streamstats import StreamStatsClient

from .errors import http_error

router = APIRouter()

# Same timeout the smoke script uses. Delineation is slow.
_TIMEOUT_S = 90


class PointBody(BaseModel):
    latitude: float
    longitude: float
    region: str = "OR"
    verify_ssl: bool = True


class CharacteristicsBody(BaseModel):
    region: str = "OR"
    bcrequest: dict
    labels: str = "*"
    verify_ssl: bool = True


class FlowStatsBody(BaseModel):
    region: str = "OR"
    basin_geojson: dict
    characteristics: list
    statistic_groups: Optional[list] = None
    verify_ssl: bool = True


def _client(verify_ssl: bool, timeout: int = _TIMEOUT_S) -> StreamStatsClient:
    return StreamStatsClient(timeout=timeout, verify_ssl=verify_ssl)


@router.post("/snap")
def snap(body: PointBody) -> dict:
    try:
        return _client(body.verify_ssl).snap(
            body.latitude, body.longitude, region=body.region
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.post("/watershed")
def watershed(body: PointBody) -> dict:
    try:
        return _client(body.verify_ssl).watershed(
            body.latitude, body.longitude, region=body.region
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.post("/characteristics")
def characteristics(body: CharacteristicsBody) -> dict:
    # A full state list can take longer than the delineate itself.
    try:
        rows = _client(body.verify_ssl, timeout=180).basin_characteristics(
            body.bcrequest,
            body.region,
            labels=body.labels,
        )
        return {"region": body.region, "characteristics": rows}
    except Exception as exc:
        raise http_error(exc) from exc


@router.post("/flowstats")
def flowstats(body: FlowStatsBody) -> dict:
    # Regions, scenarios, then the estimate. Another slow USGS round.
    try:
        rows = _client(body.verify_ssl, timeout=180).flow_statistics(
            body.basin_geojson,
            body.characteristics,
            body.region,
            statistic_groups=body.statistic_groups,
        )
        return {"region": body.region, "statistics": rows}
    except Exception as exc:
        raise http_error(exc) from exc
