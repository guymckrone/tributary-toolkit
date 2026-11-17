"""Slice 0 throwaway. Not the terrain package.

Proves whitebox_workflows can make a streams raster and a HAND
(height above nearest drainage) raster from a small real DEM,
without touching the datadownload env.

Run from data-downloader with the cloned env's Python:

    C:\\Users\\Guy.McFeeters\\miniforge3\\envs\\datadownload-geo\\python.exe terrain\\spike_slice0.py

Outputs go to the Windows temp folder, not the repo.
"""

import math
import sys
import tempfile
from pathlib import Path

import numpy as np
import requests
from PIL import Image

# This file lives in terrain/, so the folder on sys.path would be
# terrain/ and "import dem" would fail. Put the project root first.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dem.models import DemRequest
from dem.registry import create_provider
from dem.service import get_dem, list_collections

import whitebox_workflows as wbw


# Plan default AOI, sized to about 600 m on a side (inside the
# 400-800 m spike window). Degrees here are only the request box.
# get_dem snaps the real grid in UTM.
CENTER_LON = -122.59
CENTER_LAT = 45.37
HALF_M = 300.0
LOCK_KEY = "OR_PortlandMetro_B24"

# Plan's two starting guesses, in cells of accumulation. On a
# ~600 m box the second one can be larger than the whole grid.
TRIAL_THRESHOLDS = (50_000.0, 500_000.0)

# max_dist is in cells. The library default is 100. The plan's
# example from the docs is 50, so the spike starts there.
BREACH_MAX_DIST = 50


def _box():
    """Lon/lat box about 600 m across, centered on Johnson Creek."""
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(CENTER_LAT))
    half_lat = HALF_M / meters_per_deg_lat
    half_lon = HALF_M / meters_per_deg_lon
    return (
        CENTER_LON - half_lon,
        CENTER_LAT - half_lat,
        CENTER_LON + half_lon,
        CENTER_LAT + half_lat,
    )


def _provider():
    """3DEP client. Same SSL fallback the DEM smoke uses."""
    verified = create_provider("3dep", timeout=180)
    try:
        verified.session.get(
            "https://elevation.nationalmap.gov/arcgis/rest/services/"
            "3DEPElevation/ImageServer",
            params={"f": "json"},
            timeout=30,
            verify=True,
        )
    except requests.exceptions.SSLError:
        print(
            "SSL verify failed. Work-network interception. "
            "Continuing with verify_ssl=False."
        )
        return create_provider("3dep", timeout=180, verify_ssl=False)
    print("SSL verify succeeded.")
    return verified


def _as_array(raster):
    """Whitebox Raster -> 2D float64. Nodata becomes NaN when the
    reader honored the GeoTIFF tag. Sentinel -999999 stays a number
    if the tag was missed, and the caller checks for that."""
    array = np.asarray(raster.to_numpy(), dtype=np.float64)
    if array.ndim == 3:
        array = array[0]
    return array


def save_float32(wbe, raster, path):
    """Write a Float32 GeoTIFF Pillow can open.

    Whitebox's own writer emits 64-bit samples. Pillow 12 raises
    SyntaxError ("unknown pixel mode") on those, which Image.open
    then reports as an unidentified image. "float" here is Whitebox's
    name for 32-bit float, the same pixel type as the DEM download.
    The in-memory raster passed onward is still the original.
    """
    stored = raster.new_from_other_with_data(raster, "float")
    wbe.write_raster(stored, str(path))
    return path


def _report(label, path, raster):
    """Print min/max of finite cells, and prove Pillow can open the file."""
    array = _as_array(raster)
    finite = np.isfinite(array)
    count = int(finite.sum())
    sentinel = int(np.count_nonzero(finite & (np.abs(array + 999_999.0) < 1.0)))
    size = path.stat().st_size if path.is_file() else 0
    print(
        f"{label}: shape={array.shape} finite={count} "
        f"sentinel_-999999={sentinel} bytes={size}"
    )
    if count:
        values = array[finite]
        print(
            f"  min={float(values.min()):.4g} "
            f"max={float(values.max()):.4g} "
            f"mean={float(values.mean()):.4g}"
        )
    with Image.open(path) as image:
        print(f"  pillow mode={image.mode} size={image.size} format={image.format}")
    return array


def _stream_count(array):
    """Stream cells are > 0. Off-channel cells are 0
    (zero_background=True)."""
    return int(np.count_nonzero(np.isfinite(array) & (array > 0)))


def _license_line(wbe, tool_id):
    info = wbe.describe_tool(tool_id)
    print(
        f"license {tool_id}: tier={info.get('license_tier')} "
        f"is_pro={info.get('is_pro')} "
        f"available={info.get('available_in_current_session')}"
    )


def main():
    print(f"numpy {np.__version__}")
    out_dir = Path(tempfile.gettempdir()) / "terrain_spike_slice0"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"output dir {out_dir}")

    bbox = _box()
    print(f"request bbox {bbox}")
    provider = _provider()
    catalog = list_collections(bbox, provider=provider, timeout=180)
    keys = [item.key for item in catalog.collections]
    print(f"suggested {catalog.suggested_key}")
    print("catalog keys: " + ", ".join(keys))
    # The picker key is not the bare flight name. It looks like
    # "project:OR_PortlandMetro_B24|1.00". Match the name.
    match = next(
        (
            item for item in catalog.collections
            if item.name == LOCK_KEY or LOCK_KEY in item.key
        ),
        None,
    )
    if match is None:
        collection_key = catalog.suggested_key
        print(
            f"{LOCK_KEY} was not in the catalog. "
            f"Using suggested {collection_key}."
        )
    else:
        collection_key = match.key
        print(f"locked key {collection_key} name {match.name}")

    request = DemRequest(bbox=bbox, resolution_m=1.0)
    dem = get_dem(
        request,
        provider=provider,
        timeout=180,
        collection_key=collection_key,
    )
    print(
        f"dem path {dem.path}\n"
        f"  from_cache={dem.from_cache} epsg={dem.epsg} "
        f"requested_m={dem.requested_resolution_m} "
        f"source_m={dem.source_resolution_m}\n"
        f"  collection={dem.collection_key} {dem.collection_name} "
        f"{dem.collection_period}\n"
        f"  nodata={dem.nodata} vertical={dem.vertical_unit}\n"
        f"  bbox_projected={dem.bbox_projected}\n"
        f"  {dem.source_description}"
    )

    wbe = wbw.WbEnvironment()
    print(f"whitebox_workflows {wbe.version()} license {wbe.license_type()}")
    for tool_id in (
        "breach_depressions_least_cost",
        "d8_pointer",
        "d8_flow_accum",
        "extract_streams",
        "elevation_above_stream",
        "elevation_above_stream_euclidean",
        "hillshade",
    ):
        _license_line(wbe, tool_id)

    # file_mode "m" loads the grid into memory. This box is small.
    dem_raster = wbe.read_raster(dem.path, file_mode="m")
    dem_array = _report("dem", Path(dem.path), dem_raster)
    if np.count_nonzero(
        np.isfinite(dem_array) & (np.abs(dem_array + 999_999.0) < 1.0)
    ):
        raise SystemExit(
            "Whitebox read -999999 as elevation. Refusing to breach "
            "a grid full of fake pits. Stop and fix nodata first."
        )

    breached_path = out_dir / "breached.tif"
    print(
        "call wbe.hydrology.depressions_storage."
        f"breach_depressions_least_cost(max_dist={BREACH_MAX_DIST}, "
        "fill_deps=False)"
    )
    breached = wbe.hydrology.depressions_storage.breach_depressions_least_cost(
        dem=dem_raster,
        max_dist=BREACH_MAX_DIST,
        fill_deps=False,
    )
    save_float32(wbe, breached, breached_path)
    _report("breached", breached_path, breached)

    pointer_path = out_dir / "d8_pointer.tif"
    print("call wbe.hydrology.flow_routing.d8_pointer")
    pointer = wbe.hydrology.flow_routing.d8_pointer(
        dem=breached,
        esri_pntr=False,
    )
    save_float32(wbe, pointer, pointer_path)
    _report("d8_pointer", pointer_path, pointer)

    accum_path = out_dir / "d8_accum_cells.tif"
    print("call wbe.hydrology.flow_routing.d8_flow_accum(out_type='cells')")
    accum = wbe.hydrology.flow_routing.d8_flow_accum(
        input=pointer,
        out_type="cells",
        input_is_pointer=True,
        esri_pntr=False,
    )
    save_float32(wbe, accum, accum_path)
    accum_array = _report("d8_accum_cells", accum_path, accum)
    accum_max = float(np.nanmax(accum_array))
    print(f"accumulation max cells = {accum_max:.0f}")

    # A channel on this little grid is a thin line: more than a
    # short stub, and not a sheet of the whole hillside. 8,000 cells
    # is about 2% of a 600 x 600 grid.
    #
    # The plan's 50,000 and 500,000 guesses assume a DEM that still
    # contains the upstream watershed. This box does not. Flow
    # entering the edge starts at 1 cell, so the maximum here is
    # only the area that falls inside the box.
    trials = list(TRIAL_THRESHOLDS)
    for fraction in (0.02, 0.10, 0.30):
        derived = max(100.0, accum_max * fraction)
        if all(abs(derived - existing) > 1.0 for existing in trials):
            trials.append(derived)

    chosen = None
    chosen_array = None
    for threshold in trials:
        streams_path = out_dir / f"streams_{int(threshold)}.tif"
        # zero_background=True writes 0 off the channel. The default
        # (False) writes the integer nodata -32768, and the Float32
        # copy keeps that number as if it were data.
        print(
            "call wbe.streams.network_extraction.extract_streams"
            f"(threshold={threshold:g}, zero_background=True)"
        )
        streams = wbe.streams.network_extraction.extract_streams(
            flow_accumulation=accum,
            threshold=threshold,
            zero_background=True,
        )
        save_float32(wbe, streams, streams_path)
        streams_array = _report(streams_path.name, streams_path, streams)
        count = _stream_count(streams_array)
        fraction = count / streams_array.size
        print(f"  stream cells={count} fraction={fraction:.4f}")
        sane = 100 <= count <= 8_000
        print(f"  sane_channel={sane}")
        if sane and (chosen is None or threshold > chosen[0]):
            # Prefer the stricter threshold that still draws a channel,
            # so the line is the creek and not every roadside ditch.
            chosen = (threshold, streams, streams_path)
            chosen_array = streams_array

    if chosen is None:
        raise SystemExit(
            "No threshold drew a sane channel (100 to 8000 stream cells). "
            f"Accumulation max was {accum_max:.0f}."
        )

    threshold, streams, streams_path = chosen
    print(f"HAND uses threshold {threshold:g} from {streams_path.name}")

    rem_path = out_dir / "rem_flowpath.tif"
    print("call wbe.hydrology.hydrologic_indices.elevation_above_stream")
    rem = wbe.hydrology.hydrologic_indices.elevation_above_stream(
        dem=breached,
        streams=streams,
    )
    save_float32(wbe, rem, rem_path)
    rem_array = _report("rem_flowpath", rem_path, rem)
    on_stream = (
        np.isfinite(chosen_array) & (chosen_array > 0) & np.isfinite(rem_array)
        & (rem_array > -1000)
    )
    off_stream = np.isfinite(rem_array) & (rem_array > -1000) & ~on_stream
    no_path = np.isfinite(rem_array) & (rem_array <= -1000)
    print(f"  REM cells with no downslope path to a stream={int(no_path.sum())}")
    if on_stream.any():
        print(
            "  REM on stream cells "
            f"median={float(np.median(rem_array[on_stream])):.4g} "
            f"max={float(rem_array[on_stream].max()):.4g}"
        )
    if off_stream.any():
        print(
            "  REM off stream "
            f"median={float(np.median(rem_array[off_stream])):.4g} "
            f"p90={float(np.percentile(rem_array[off_stream], 90)):.4g}"
        )

    rem_euc_path = out_dir / "rem_euclidean.tif"
    print(
        "call wbe.hydrology.hydrologic_indices."
        "elevation_above_stream_euclidean"
    )
    rem_euc = wbe.hydrology.hydrologic_indices.elevation_above_stream_euclidean(
        dem=breached,
        streams=streams,
    )
    save_float32(wbe, rem_euc, rem_euc_path)
    _report("rem_euclidean", rem_euc_path, rem_euc)

    shade_path = out_dir / "hillshade.tif"
    print("call wbe.terrain.hillshade (azimuth=315, altitude=30, z_factor=1)")
    shade = wbe.terrain.hillshade(
        input=dem_raster,
        azimuth=315.0,
        altitude=30.0,
        z_factor=1.0,
    )
    save_float32(wbe, shade, shade_path)
    _report("hillshade", shade_path, shade)

    print(f"numpy still imports: {np.__version__}")
    print("SPIKE_OK")


if __name__ == "__main__":
    main()
