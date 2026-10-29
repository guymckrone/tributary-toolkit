"""Folium map for the StreamStats page.

Plain Folium only — no Streamlit in here. The page calls
st_folium on whatever I return.

I draw:
    picked point     — current sidebar lat/lon, including a map
                       click that hasn't been sent yet (orange)
    snapped point    — where SS-Delineate put the pour point (blue)
    basin polygon    — globalwatershed Feature
"""

from typing import Any, Dict, List, Optional, Tuple

import folium

# Oregon-ish center when there's nothing to show yet
# (same idea as the empty draw map on Data Download).
_EMPTY_CENTER = (44.0, -120.5)
_EMPTY_ZOOM = 6
_POINT_ZOOM = 13

_REQUEST_COLOR = "#ff7f0e"  # orange
_SNAP_COLOR = "#1f77b4"     # blue
_BASIN_COLOR = "#2ca02c"    # green


def _positions(coordinates: Any) -> List[Tuple[float, float]]:
    """Flatten nested GeoJSON rings into (lon, lat) pairs."""
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        return []
    # A position is [lon, lat, …] — numbers, not nested lists.
    if not isinstance(coordinates[0], (list, tuple)):
        return [(float(coordinates[0]), float(coordinates[1]))]
    out: List[Tuple[float, float]] = []
    for part in coordinates:
        out.extend(_positions(part))
    return out


def _basin_bounds(
    basin_geojson: Dict[str, Any],
) -> Optional[List[List[float]]]:
    """[[south, west], [north, east]] for fit_bounds, or None."""
    geometry = (basin_geojson or {}).get("geometry") or {}
    pts = _positions(geometry.get("coordinates"))
    if not pts:
        return None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def _add_point(
    fmap: folium.Map,
    lat: float,
    lon: float,
    color: str,
    label: str,
) -> None:
    folium.CircleMarker(
        location=(lat, lon),
        radius=7,
        color=color,
        fill=True,
        fill_color=color,
        fill_opacity=0.9,
        weight=2,
        tooltip=f"{label}: {lat:.6f}, {lon:.6f}",
    ).add_to(fmap)


def _include_point(
    bounds: Optional[List[List[float]]],
    lat: Optional[float],
    lon: Optional[float],
) -> Optional[List[List[float]]]:
    """Stretch a fit_bounds box so the picked point stays on screen."""
    if lat is None or lon is None:
        return bounds
    if not bounds:
        return [[lat, lon], [lat, lon]]
    south, west = bounds[0]
    north, east = bounds[1]
    return [
        [min(south, lat), min(west, lon)],
        [max(north, lat), max(east, lon)],
    ]


def build_basin_map(
    snap: Optional[Dict[str, Any]] = None,
    watershed: Optional[Dict[str, Any]] = None,
    picked_lat: Optional[float] = None,
    picked_lon: Optional[float] = None,
) -> folium.Map:
    """Map of the current point plus the latest snap and/or basin.

    picked_lat / picked_lon: sidebar values (typed or map-clicked).
        Always drawn, even before Snap — that's the point that
        the next button press will send.
    snap: dict shaped like StreamStatsClient.snap().
    watershed: dict shaped like StreamStatsClient.watershed().
        With a basin I fit the view to it (and to the picked
        point, so a click outside the basin doesn't disappear).
        Otherwise I center on the picked point, or on Oregon.
    """
    basin_geojson = (watershed or {}).get("basin_geojson")
    bounds = _basin_bounds(basin_geojson) if basin_geojson else None
    # A lone point makes a zero-area box; only fit when the
    # basin actually has extent. A click with no basin just
    # recenters via location/zoom below.
    fit = bounds is not None
    if fit:
        bounds = _include_point(bounds, picked_lat, picked_lon)

    if picked_lat is not None and picked_lon is not None:
        center = (picked_lat, picked_lon)
        zoom = _POINT_ZOOM
    elif snap:
        center = (snap["latitude"], snap["longitude"])
        zoom = _POINT_ZOOM
    else:
        center = _EMPTY_CENTER
        zoom = _EMPTY_ZOOM

    fmap = folium.Map(location=center, zoom_start=zoom)

    # Basin first so the points draw on top of it.
    if basin_geojson:
        folium.GeoJson(
            basin_geojson,
            name="Watershed",
            style_function=lambda _feature: {
                "color": _BASIN_COLOR,
                "weight": 2,
                "fillColor": _BASIN_COLOR,
                "fillOpacity": 0.2,
            },
            tooltip="Watershed",
            # Same idea as the markers — a click inside the
            # basin should still move the pour point.
            interactive=False,
        ).add_to(fmap)

    if picked_lat is not None and picked_lon is not None:
        _add_point(
            fmap,
            picked_lat,
            picked_lon,
            _REQUEST_COLOR,
            "Selected",
        )
    if snap:
        _add_point(
            fmap,
            snap["latitude"],
            snap["longitude"],
            _SNAP_COLOR,
            "Snapped",
        )

    if fit and bounds:
        fmap.fit_bounds(bounds)

    return fmap
