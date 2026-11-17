"""Terrain tools backend. Shared prep first; REM, profiles, and
inundation build on it later.

Sibling of dem/, not part of it. terrain/ may import dem/, never
the other way. Whitebox is only imported when prep actually runs,
so this package loads in datadownload too. Running prep needs
datadownload-geo.
"""

from .cache import clear_cache, default_cache_dir, prep_cache_key
from .dem_input import DemInput, file_identity, resolve_dem
from .models import PrepParams, PrepResult, TerrainAoi
from .prep import WHITEBOX_NODATA
from .service import prepare_terrain

__all__ = [
    "DemInput",
    "PrepParams",
    "PrepResult",
    "TerrainAoi",
    "WHITEBOX_NODATA",
    "clear_cache",
    "default_cache_dir",
    "file_identity",
    "prep_cache_key",
    "prepare_terrain",
    "resolve_dem",
]
