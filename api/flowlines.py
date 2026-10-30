"""On-demand NHD flowlines for the StreamStats map.

These are for aiming a pour point. Snap still goes through
SS-Delineate — I never snap to a vertex from this layer.

Service: National Map NHD MapServer layer 6 (flowlines).
A response is capped at 2000 features. I cache by a bbox
expanded out to the nearest 0.01 degrees so a tiny pan
doesn't refetch and streams on the edge stay included.
"""

import math
from functools import lru_cache
from typing import Any, Dict, Tuple

import requests
import urllib3

_QUERY = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/"
    "nhd/MapServer/6/query"
)
_CAP = 2000
_TIMEOUT_S = 60


def _where_for_span(span: float) -> str:
    """Fewer, larger streams when the view is wide.

    visibilityFilter is the National Map's own detail flag.
    If the field name is rejected, the caller retries with
    where=1=1 and the record cap still applies.
    """
    if span >= 2.0:
        return "visibilityFilter >= 1000000"
    if span >= 0.4:
        return "visibilityFilter >= 500000"
    return "visibilityFilter >= 100000"


def _query(
    west: float,
    south: float,
    east: float,
    north: float,
    where: str,
) -> Dict[str, Any]:
    # bbox is west,south,east,north (minx,miny,maxx,maxy).
    # I always skip SSL checks here. The work network's proxy breaks
    # hydro.nationalmap.gov, and this is public read-only data.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.get(
        _QUERY,
        params={
            "where": where,
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "gnis_name,reachcode,ftype",
            "returnGeometry": "true",
            "f": "geojson",
            "resultRecordCount": _CAP,
        },
        timeout=_TIMEOUT_S,
        headers={"User-Agent": "TributaryToolkit flowlines"},
        verify=False,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error") and "features" not in payload:
        message = payload["error"]
        if isinstance(message, dict):
            message = message.get("message") or message
        raise ValueError(str(message))
    return payload


@lru_cache(maxsize=32)
def _cached(
    west: float,
    south: float,
    east: float,
    north: float,
    where: str,
) -> Tuple[int, bool, str]:
    """Return (count, capped, geojson text). Text so the cache stays simple."""
    import json

    try:
        payload = _query(west, south, east, north, where)
    except ValueError:
        if where == "1=1":
            raise
        payload = _query(west, south, east, north, "1=1")

    features = payload.get("features") or []
    if not isinstance(payload.get("type"), str):
        payload = {"type": "FeatureCollection", "features": features}
    count = len(features)
    return count, count >= _CAP, json.dumps(payload)


def fetch_flowlines(
    west: float,
    south: float,
    east: float,
    north: float,
) -> Dict[str, Any]:
    """GeoJSON for one view, plus whether the 2000 cap was hit."""
    import json

    if west >= east or south >= north:
        raise ValueError(
            "Bounding box is empty or flipped. "
            "west < east and south < north."
        )
    span = max(abs(east - west), abs(north - south))
    where = _where_for_span(span)
    # Expand to a 0.01° grid so a small pan reuses the cache,
    # and so rounding never clips streams off the view edge.
    # The epsilon keeps a value that is already on the grid
    # (but stored as 1.2300000001) from stepping an extra cell.
    w = round(math.floor(west * 100 + 1e-8) / 100, 2)
    s = round(math.floor(south * 100 + 1e-8) / 100, 2)
    e = round(math.ceil(east * 100 - 1e-8) / 100, 2)
    n = round(math.ceil(north * 100 - 1e-8) / 100, 2)
    # A view sitting on one grid line can collapse. Nudge it open.
    if w >= e:
        e = round(w + 0.01, 2)
    if s >= n:
        n = round(s + 0.01, 2)
    count, capped, text = _cached(w, s, e, n, where)
    return {
        "geojson": json.loads(text),
        "count": count,
        "capped": capped,
    }
