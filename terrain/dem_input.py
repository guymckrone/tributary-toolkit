"""TerrainAoi -> a GeoTIFF on disk.

I don't talk to 3DEP here. A bbox goes through dem.get_dem, so
the DEM cache, collection lock, fill, and clip all behave the
way they do on the DEM page. A dem_path is used as it is.

Identity is a hash of the file bytes, not the path. A DEM cache
file can be cleared and re-downloaded under the same name with
newer lidar, and I don't want prep from the old one to hit.
Hashing also means the same file reached by a bbox or by its
path is one prep cache entry.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dem.models import DemResult
from dem.service import get_dem

from .models import TerrainAoi

_CHUNK = 1 << 20


@dataclass(frozen=True)
class DemInput:
    path: str
    identity: str
    # None for a dem_path. I only know the file then.
    dem: Optional[DemResult] = None


def file_identity(path) -> str:
    """sha256 of the bytes, first 16 hex characters."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()[:16]


def resolve_dem(
    aoi: TerrainAoi,
    dem_cache_dir=None,
    provider=None,
    timeout: int = 90,
) -> DemInput:
    """The local DEM for this AOI. Downloads only on a DEM cache miss."""
    if aoi.dem_path:
        path = Path(aoi.dem_path)
        if not path.is_file():
            raise ValueError(f"dem_path does not exist: {path}")
        with path.open("rb") as handle:
            magic = handle.read(4)
        # Little-endian TIFF, same check as the DEM cache. A big-endian
        # or BigTIFF file would need its own test before I accept it.
        if magic != b"II*\x00":
            raise ValueError(f"dem_path is not a GeoTIFF: {path}")
        return DemInput(path=str(path.resolve()), identity=file_identity(path))

    dem = get_dem(
        aoi.dem_request(),
        max_mb=aoi.max_mb,
        cache_dir=dem_cache_dir,
        provider=provider,
        timeout=timeout,
        verify_ssl=aoi.verify_ssl,
        collection_key=aoi.collection_key,
        fill_key=aoi.fill_key,
        clip=aoi.clip,
    )
    return DemInput(path=dem.path, identity=file_identity(dem.path), dem=dem)
