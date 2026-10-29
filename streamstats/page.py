"""StreamStats page body (Phase 1 — one pour point).

pages/02_StreamStats.py is a thin wrapper that calls main().

Flow:
    sidebar lat / lon / region, or a click on the map
    → Snap or Delineate button
    → StreamStatsClient → result saved in session_state
    → map + tables + GeoJSON download read from session_state

A map click only fills the lat/lon boxes. It does not call
USGS — Snap / Delineate still do that.

Every session key starts with ss_ so I never clobber Data
Download state (discovery_*, download_*, source_*).

API calls only happen inside button branches. Streamlit
reruns this whole script on every widget change; if the
call lived outside a button I'd hit USGS on every keystroke.
"""

import json
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

from .client import StreamStatsClient
from .map import build_basin_map

# Johnson Creek area — same on-network point the smoke uses.
_DEFAULT_LAT = 45.3725
_DEFAULT_LON = -122.5858
_DEFAULT_REGION = "OR"

# Smoke uses 90s; SS-Delineate can take most of a minute.
_TIMEOUT_S = 90

# session_state keys (all ss_*)
_K_SNAP = "ss_snap"
_K_WATERSHED = "ss_watershed"
_K_ERROR = "ss_error"
# Click from the previous run, applied before the lat/lon
# widgets exist. Streamlit refuses to change a widget's
# value after that widget has already been drawn.
_K_PENDING_CLICK = "ss_pending_click"
# Last click I already copied into the boxes. st_folium
# keeps returning the same last_clicked on later reruns;
# without this I'd loop forever.
_K_CLICK_TOKEN = "ss_click_token"


def _prepare_point_inputs() -> None:
    """Apply a map click from the last run, then seed defaults."""
    pending = st.session_state.pop(_K_PENDING_CLICK, None)
    if pending is not None:
        st.session_state["ss_lat"] = pending[0]
        st.session_state["ss_lon"] = pending[1]
    if "ss_lat" not in st.session_state:
        st.session_state["ss_lat"] = _DEFAULT_LAT
    if "ss_lon" not in st.session_state:
        st.session_state["ss_lon"] = _DEFAULT_LON


def _lat_lon_from_click(
    map_data: Optional[Dict[str, Any]],
) -> Optional[Tuple[float, float]]:
    """(lat, lon) from st_folium's last_clicked, or None.

    Rounded to 6 decimals so the number boxes and the
    "already handled" token agree. ~0.1 m.
    """
    clicked = (map_data or {}).get("last_clicked") or {}
    lat = clicked.get("lat")
    lon = clicked.get("lng")
    if lat is None or lon is None:
        return None
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    return (round(lat_f, 6), round(lon_f, 6))


def _queue_map_click(map_data: Optional[Dict[str, Any]]) -> bool:
    """Stash a new map click and ask for a rerun. True if queued.

    Must run after the map, so it cannot write ss_lat itself —
    those widgets already exist this run. Next run,
    _prepare_point_inputs copies the stash in first.
    """
    picked = _lat_lon_from_click(map_data)
    if picked is None:
        return False
    if st.session_state.get(_K_CLICK_TOKEN) == picked:
        return False
    st.session_state[_K_CLICK_TOKEN] = picked
    st.session_state[_K_PENDING_CLICK] = picked
    return True


def _sidebar_inputs() -> Dict[str, Any]:
    """Lat / lon / region / SSL toggle. No API calls here.

    Call _prepare_point_inputs() first so a map click can set
    ss_lat / ss_lon. I don't pass value= on those two — the
    key already holds the number, and passing both makes
    Streamlit warn.
    """
    st.sidebar.header("Pour point")
    st.sidebar.caption("Type a point, or click the map.")
    lat = st.sidebar.number_input(
        "Latitude",
        min_value=-90.0,
        max_value=90.0,
        step=0.0001,
        format="%.6f",
        key="ss_lat",
    )
    lon = st.sidebar.number_input(
        "Longitude",
        min_value=-180.0,
        max_value=180.0,
        step=0.0001,
        format="%.6f",
        key="ss_lon",
    )
    region = st.sidebar.text_input(
        "Region (state code)",
        value=_DEFAULT_REGION,
        help=(
            "SS-Delineate is split by state. Cross-state basins "
            "aren't supported by the service."
        ),
        key="ss_region",
    )
    verify_ssl = st.sidebar.checkbox(
        "Verify SSL",
        value=True,
        help="Turn off on the work network if HTTPS gets intercepted.",
        key="ss_verify_ssl",
    )
    return {
        "lat": float(lat),
        "lon": float(lon),
        "region": region,
        "verify_ssl": verify_ssl,
    }


def _client(verify_ssl: bool) -> StreamStatsClient:
    # Cheap to build — no network until a method is called.
    return StreamStatsClient(timeout=_TIMEOUT_S, verify_ssl=verify_ssl)


def _same_request(
    snap: Optional[Dict[str, Any]],
    other: Optional[Dict[str, Any]],
) -> bool:
    """True when two snap dicts came from the same typed point."""
    if not snap or not other:
        return False
    return (
        snap["request_latitude"] == other["request_latitude"]
        and snap["request_longitude"] == other["request_longitude"]
        and snap["region"] == other["region"]
    )


def _run_snap(inputs: Dict[str, Any]) -> None:
    """Snap button → ss_snap. On failure keep the last good result."""
    try:
        with st.spinner("Snapping to the stream network…"):
            snap = _client(inputs["verify_ssl"]).snap(
                inputs["lat"], inputs["lon"], region=inputs["region"]
            )
    except (ValueError, requests.RequestException) as exc:
        st.session_state[_K_ERROR] = f"Snap failed: {exc}"
        return

    st.session_state[_K_SNAP] = snap
    st.session_state.pop(_K_ERROR, None)

    # A new snap at a different point would leave the old basin
    # on the map next to the wrong pour point — drop it.
    ws = st.session_state.get(_K_WATERSHED)
    if ws and not _same_request(snap, ws.get("snap")):
        st.session_state.pop(_K_WATERSHED, None)


def _run_watershed(inputs: Dict[str, Any]) -> None:
    """Delineate button → ss_watershed (+ ss_snap from the same call)."""
    try:
        with st.spinner(
            "Delineating watershed… this can take most of a minute."
        ):
            result = _client(inputs["verify_ssl"]).watershed(
                inputs["lat"], inputs["lon"], region=inputs["region"]
            )
    except (ValueError, requests.RequestException) as exc:
        st.session_state[_K_ERROR] = f"Delineate failed: {exc}"
        return

    st.session_state[_K_WATERSHED] = result
    # Snap rides along in the watershed response — keep them in
    # sync so the map never mixes two different pour points.
    st.session_state[_K_SNAP] = result["snap"]
    st.session_state.pop(_K_ERROR, None)


def _show_snap(snap: Dict[str, Any]) -> None:
    st.subheader("Snapped pour point")
    rows = [
        ("Requested latitude", f"{snap['request_latitude']:.6f}"),
        ("Requested longitude", f"{snap['request_longitude']:.6f}"),
        ("Snapped latitude", f"{snap['latitude']:.6f}"),
        ("Snapped longitude", f"{snap['longitude']:.6f}"),
        ("Region", snap["region"]),
        ("Snappable", str(snap["snappable"])),
        ("HUC", snap.get("huc_id") or ""),
        ("Warning", snap.get("warning") or ""),
    ]
    st.table(pd.DataFrame(rows, columns=["Field", "Value"]))
    if not snap["snappable"]:
        st.warning(
            "StreamStats says this point isn't snappable. "
            "Try a point closer to a mapped stream."
        )


def _show_watershed(ws: Dict[str, Any]) -> None:
    st.subheader("Watershed")
    rows = [
        ("Geometry type", ws.get("geometry_type") or ""),
        ("Vertex count", str(ws.get("vertex_count") or 0)),
        ("Shape_Area", str(ws.get("shape_area") or "")),
        ("Warning", ws.get("warning") or ""),
    ]
    st.table(pd.DataFrame(rows, columns=["Field", "Value"]))
    st.caption(
        "Shape_Area is whatever SS-Delineate reports — "
        "I haven't confirmed its units yet."
    )

    st.download_button(
        "Download GeoJSON",
        data=json.dumps(ws["basin_geojson"]),
        file_name="watershed.geojson",
        mime="application/geo+json",
        key="ss_download_geojson",
    )


def main() -> None:
    st.set_page_config(
        page_title="StreamStats — Tributary Toolkit",
        layout="wide",
    )

    st.title("StreamStats")
    st.write(
        "Snap a point to the stream network and delineate its "
        "watershed via USGS SS-Delineate. Click the map to "
        "set the point — that does not call USGS."
    )

    _prepare_point_inputs()
    inputs = _sidebar_inputs()

    col_snap, col_ws = st.columns(2)
    with col_snap:
        if st.button("Snap", key="ss_btn_snap", use_container_width=True):
            _run_snap(inputs)
    with col_ws:
        if st.button(
            "Delineate watershed",
            key="ss_btn_watershed",
            type="primary",
            use_container_width=True,
        ):
            _run_watershed(inputs)

    err = st.session_state.get(_K_ERROR)
    if err:
        st.error(err)

    snap = st.session_state.get(_K_SNAP)
    ws = st.session_state.get(_K_WATERSHED)

    col_info, col_map = st.columns([1, 2])
    with col_info:
        if snap:
            _show_snap(snap)
        if ws:
            _show_watershed(ws)
        if not snap and not ws:
            st.info(
                "Click the map or type a point, then **Snap** "
                "or **Delineate watershed**."
            )

    with col_map:
        st.subheader("Map")
        st.caption(
            "Click to set the pour point (orange). "
            "Blue = snapped · Green = watershed. "
            "A click only updates lat/lon."
        )
        fmap = build_basin_map(
            snap=snap,
            watershed=ws,
            picked_lat=inputs["lat"],
            picked_lon=inputs["lon"],
        )
        # last_clicked only — I don't ask for center/zoom, so
        # panning shouldn't rerun the script. A click does.
        map_data = st_folium(
            fmap,
            key="ss_map",
            height=520,
            use_container_width=True,
            returned_objects=["last_clicked"],
        )
        if _queue_map_click(map_data):
            st.rerun()
