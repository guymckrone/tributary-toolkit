"""Breach -> D8 pointer -> accumulation -> streams, with Whitebox Next Gen.

The only module that imports whitebox_workflows, and only when a
run starts. models, cache, and service still import in the plain
datadownload env, where Whitebox is not installed.

Calls are the ones Slice 0 proved (Nov 12, 2026). Keyword
arguments only; a positional argument raises TypeError.
"""

from pathlib import Path

import numpy as np

from .cache import PREP_FILES
from .models import PrepParams

# Whitebox writes this in pointer, accumulation, and streams
# wherever the DEM had nodata. Probed Nov 16 on a synthetic DEM
# with one -999999 corner.
WHITEBOX_NODATA = -32768.0
# The DEM package's missing elevation (dem.composite.NODATA).
DEM_NODATA = -999999.0


def whitebox_environment():
    """A WbEnvironment, or a clear error if this env can't make one."""
    try:
        import whitebox_workflows as wbw
    except ImportError as exc:
        raise ImportError(
            "whitebox_workflows is not installed in this env. Terrain "
            "prep runs in datadownload-geo: "
            "conda run -n datadownload-geo python ..."
        ) from exc
    return wbw.WbEnvironment()


def save_float32(wbe, raster, path: Path) -> Path:
    """Write a Float32 GeoTIFF Pillow can open.

    Whitebox's own writer emits 64-bit samples, and Pillow 12 can't
    read those (Slice 0). "float" is Whitebox's 32-bit type, the
    same pixel type as the DEM download.
    """
    stored = raster.new_from_other_with_data(raster, "float")
    wbe.write_raster(stored, str(path))
    return path


def _array(raster) -> np.ndarray:
    values = np.asarray(raster.to_numpy(), dtype=np.float64)
    if values.ndim == 3:
        values = values[0]
    return values


def check_dem(dem_raster) -> np.ndarray:
    """The DEM as an array. Raises if Whitebox read nodata as ground.

    to_numpy() hands back -999999 as a number either way. What
    matters is whether Whitebox counted those cells as valid. If
    it did, breach would carve toward a fake -1 km pit.
    """
    values = _array(dem_raster)
    sentinel = int(np.count_nonzero(np.abs(values - DEM_NODATA) < 1.0))
    valid = int(dem_raster.num_valid_cells())
    if valid == 0:
        raise ValueError("The DEM has no valid cells. Nothing to prep.")
    if values.size - valid < sentinel:
        raise ValueError(
            f"Whitebox read {sentinel} cells of {DEM_NODATA:.0f} as "
            "elevation. The GeoTIFF is missing its nodata tag. "
            "Refusing to breach a grid of fake pits."
        )
    return values


def cell_size(raster) -> float:
    """Pixel width in the raster's own units (meters for UTM)."""
    return abs(float(raster.get_x_from_column(1)) - float(raster.get_x_from_column(0)))


def run_prep(dem_path, params: PrepParams, out_dir: Path, wbe=None) -> dict:
    """Run prep on one DEM and write the four rasters into out_dir.

    Returns the numbers PrepResult needs. Raises ValueError, with
    the accumulation max in the message, when the threshold draws
    no stream cells. A blank streams raster isn't a prep.
    """
    if wbe is None:
        wbe = whitebox_environment()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: out_dir / file_name for name, file_name in PREP_FILES.items()}

    # "m" loads the grid into memory. The DEM cap keeps it at
    # 250 MB of Float32 or less.
    dem_raster = wbe.read_raster(str(dem_path), file_mode="m")
    dem_values = check_dem(dem_raster)
    epsg = int(dem_raster.crs_epsg())
    size = cell_size(dem_raster)

    breached = wbe.hydrology.depressions_storage.breach_depressions_least_cost(
        dem=dem_raster,
        max_dist=params.breach_dist,
        fill_deps=params.fill_deps,
    )
    save_float32(wbe, breached, paths["breached_path"])

    pointer = wbe.hydrology.flow_routing.d8_pointer(dem=breached, esri_pntr=False)
    save_float32(wbe, pointer, paths["pointer_path"])

    accum = wbe.hydrology.flow_routing.d8_flow_accum(
        input=pointer,
        out_type="cells",
        input_is_pointer=True,
        esri_pntr=False,
    )
    save_float32(wbe, accum, paths["accum_path"])
    # Every valid cell counts itself, so valid accumulation is >= 1.
    # Nodata is -32768, which > 0 drops.
    accum_values = _array(accum)
    valid_accum = accum_values[accum_values > 0]
    if valid_accum.size == 0:
        raise ValueError("Flow accumulation has no valid cells.")
    accum_max = float(valid_accum.max())

    if params.accum_threshold is not None:
        threshold = params.accum_threshold
    else:
        threshold = accum_max * params.accum_fraction

    # zero_background=True writes 0 off the channel. The default
    # writes -32768 there too, and then off-channel and no-DEM look
    # the same.
    streams = wbe.streams.network_extraction.extract_streams(
        flow_accumulation=accum,
        threshold=float(threshold),
        zero_background=True,
    )
    stream_cells = int(np.count_nonzero(_array(streams) > 0))
    if stream_cells == 0:
        raise ValueError(
            f"Threshold {threshold:g} cells drew no streams. This DEM's "
            f"accumulation max is {accum_max:.0f} cells. Pick a "
            "threshold under that, or use accum_fraction "
            "(0.10 gave a fuller network and 0.30 a single thread on "
            "a 600 m, 1 m lidar box)."
        )
    save_float32(wbe, streams, paths["streams_path"])

    return {
        "paths": {name: str(path) for name, path in paths.items()},
        "epsg": epsg,
        "cell_size_m": size,
        "rows": int(dem_values.shape[0]),
        "columns": int(dem_values.shape[1]),
        "accum_max": accum_max,
        "accum_threshold": float(threshold),
        "stream_cells": stream_cells,
        "nodata": WHITEBOX_NODATA,
        "whitebox_version": str(wbe.version()),
    }
