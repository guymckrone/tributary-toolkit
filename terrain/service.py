"""The terrain entry points callers (and later api/terrain.py) use.

Order: DEM (get_dem or a file), then the prep cache, then
Whitebox. A cache hit never starts Whitebox.
"""

from pathlib import Path
from typing import Optional

from .cache import (
    default_cache_dir,
    discard_entry,
    entry_dir,
    lookup_prep,
    prep_cache_key,
    store_prep,
)
from .dem_input import resolve_dem
from .models import PrepParams, PrepResult, TerrainAoi
from .prep import run_prep


def prepare_terrain(
    aoi: TerrainAoi,
    params: PrepParams,
    cache_dir: Optional[Path] = None,
    dem_cache_dir: Optional[Path] = None,
    provider=None,
    timeout: int = 90,
    wbe=None,
) -> PrepResult:
    """Breached DEM, D8 pointer, accumulation, and streams for this AOI.

    The second identical call is a cache hit (from_cache=True).
    "Identical" means the same DEM bytes and the same params, so
    a bbox and the dem_path of the file it downloaded share one
    entry.

    Raises ValueError when the threshold draws no streams, and
    nothing is cached. dem_cache_dir and provider pass through to
    get_dem, the same way the DEM smoke passes them.
    """
    dem_input = resolve_dem(
        aoi, dem_cache_dir=dem_cache_dir, provider=provider, timeout=timeout
    )
    directory = Path(cache_dir) if cache_dir else default_cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    key = prep_cache_key(dem_input.identity, params)
    cached = lookup_prep(directory, key)
    if cached is not None:
        return cached

    # A folder without prep.json is a crashed run. Start clean.
    discard_entry(directory, key)
    try:
        ran = run_prep(dem_input.path, params, entry_dir(directory, key), wbe=wbe)
    except Exception:
        discard_entry(directory, key)
        raise

    dem = dem_input.dem
    result = PrepResult(
        cache_key=key,
        dem_path=dem_input.path,
        dem_identity=dem_input.identity,
        epsg=ran["epsg"],
        cell_size_m=ran["cell_size_m"],
        rows=ran["rows"],
        columns=ran["columns"],
        accum_max=ran["accum_max"],
        accum_threshold=ran["accum_threshold"],
        accum_fraction=params.accum_fraction,
        stream_cells=ran["stream_cells"],
        breach_dist=params.breach_dist,
        fill_deps=params.fill_deps,
        nodata=ran["nodata"],
        whitebox_version=ran["whitebox_version"],
        dem_source=None if dem is None else dem.source_description,
        dem_collection_name=None if dem is None else dem.collection_name,
        dem_source_resolution_m=None if dem is None else dem.source_resolution_m,
        **ran["paths"],
    )
    return store_prep(directory, result)


__all__ = ["prepare_terrain"]
