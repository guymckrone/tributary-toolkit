"""On-disk GeoTIFF cache. No network.

The key is the request as asked (provider, resolution, rounded
bbox, CRS), not the resolved pixel size. That way a repeat of
the same call returns before the catalog is touched. "best" and
an explicit 1 m are different entries on purpose: "best" can
change when USGS publishes new lidar, and I don't want a 1 m
request to silently receive a stale "best".

A locked collection (and its fill) is appended only when the
caller passed one. An unlocked call keeps the old key, so files
cached before the lock still hit.

Files are <key>.tif and <key>.json. The JSON is the DemResult
fields that describe the raster. path and from_cache are not
stored; those depend on this machine and this read.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from .models import DemRequest, DemResult

# 6 decimals is about 0.1 m. Two boxes that close are the same
# download, and the text form stays stable.
_BBOX_DECIMALS = 6


def default_cache_dir() -> Path:
    """dem/data/cache next to this file. Gitignored except the README."""
    return Path(__file__).resolve().parent / "data" / "cache"


def cache_key(
    request: DemRequest,
    collection_key: Optional[str] = None,
    fill_key: Optional[str] = None,
    clip_token: Optional[str] = None,
) -> str:
    """Stable id for this request. Same ask, same key."""
    if isinstance(request.resolution_m, float):
        # 1 and 1.0 must collide. Four decimals is finer than
        # any pixel size I expect to request.
        resolution = f"{request.resolution_m:.4f}"
    else:
        resolution = str(request.resolution_m)
    west, south, east, north = (
        round(float(value), _BBOX_DECIMALS) for value in request.bbox
    )
    lines = [
        f"provider={request.provider}",
        f"resolution={resolution}",
        f"bbox={west:.6f},{south:.6f},{east:.6f},{north:.6f}",
        f"crs={request.out_crs}",
    ]
    # Only when set, so the unlocked key stays what it was.
    if collection_key:
        lines.append(f"collection={collection_key}")
    if fill_key:
        lines.append(f"fill={fill_key}")
    if clip_token:
        lines.append(f"clip={clip_token}")
    canonical = "\n".join(lines)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:16]


def _paths(cache_dir: Path, key: str):
    return cache_dir / f"{key}.tif", cache_dir / f"{key}.json"


def _tiff_magic_ok(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"II*\x00"
    except OSError:
        return False


def lookup(
    request: DemRequest,
    cache_dir: Path,
    collection_key: Optional[str] = None,
    fill_key: Optional[str] = None,
    clip_token: Optional[str] = None,
) -> Optional[DemResult]:
    """The cached raster, or None on a miss or a broken pair.

    A JSON without a TIFF, or a TIFF that isn't a TIFF, is a miss.
    The next download overwrites it.
    """
    tif_path, json_path = _paths(
        cache_dir, cache_key(request, collection_key, fill_key, clip_token)
    )
    if not json_path.is_file() or not tif_path.is_file():
        return None
    if not _tiff_magic_ok(tif_path):
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        projected = tuple(float(value) for value in data["bbox_projected"])
        if len(projected) != 4:
            return None
        nodata = data.get("nodata")
        return DemResult(
            path=str(tif_path),
            provider=str(data["provider"]),
            requested_resolution_m=float(data["requested_resolution_m"]),
            source_resolution_m=data["source_resolution_m"],
            coarsest_resolution_m=data["coarsest_resolution_m"],
            epsg=int(data["epsg"]),
            bbox_projected=projected,
            vertical_unit=str(data["vertical_unit"]),
            source_description=str(data["source_description"]),
            from_cache=True,
            collection_key=_optional_text(data, "collection_key"),
            collection_name=_optional_text(data, "collection_name"),
            collection_period=_optional_text(data, "collection_period"),
            fill_key=_optional_text(data, "fill_key"),
            fill_name=_optional_text(data, "fill_name"),
            fill_period=_optional_text(data, "fill_period"),
            fill_used=bool(data.get("fill_used", False)),
            nodata=None if nodata is None else float(nodata),
            clip_name=_optional_text(data, "clip_name"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _optional_text(data: dict, key: str) -> Optional[str]:
    """Sidecars from before the lock simply omit these keys."""
    value = data.get(key)
    if value is None or value == "":
        return None
    return str(value)


def store(
    request: DemRequest,
    result: DemResult,
    cache_dir: Path,
    collection_key: Optional[str] = None,
    fill_key: Optional[str] = None,
    clip_token: Optional[str] = None,
) -> DemResult:
    """Write the sidecar for a TIFF that is already at the cache path.

    The caller fetches straight into <key>.tif. I write the JSON
    after that, so a crash mid-download leaves no sidecar and the
    next call treats it as a miss.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    tif_path, json_path = _paths(
        cache_dir, cache_key(request, collection_key, fill_key, clip_token)
    )
    if Path(result.path).resolve() != tif_path.resolve():
        raise ValueError(
            f"Cached TIFF should be {tif_path}, fetch wrote {result.path}."
        )
    if not _tiff_magic_ok(tif_path):
        raise ValueError(f"Refusing to cache a non-TIFF: {tif_path}")
    payload = {
        "provider": result.provider,
        "requested_resolution_m": result.requested_resolution_m,
        "source_resolution_m": result.source_resolution_m,
        "coarsest_resolution_m": result.coarsest_resolution_m,
        "epsg": result.epsg,
        "bbox_projected": list(result.bbox_projected),
        "vertical_unit": result.vertical_unit,
        "source_description": result.source_description,
        "collection_key": result.collection_key,
        "collection_name": result.collection_name,
        "collection_period": result.collection_period,
        "fill_key": result.fill_key,
        "fill_name": result.fill_name,
        "fill_period": result.fill_period,
        "fill_used": result.fill_used,
        "nodata": result.nodata,
        "clip_name": result.clip_name,
    }
    temporary = json_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(json_path)
    return DemResult(
        path=str(tif_path),
        provider=result.provider,
        requested_resolution_m=result.requested_resolution_m,
        source_resolution_m=result.source_resolution_m,
        coarsest_resolution_m=result.coarsest_resolution_m,
        epsg=result.epsg,
        bbox_projected=result.bbox_projected,
        vertical_unit=result.vertical_unit,
        source_description=result.source_description,
        from_cache=False,
        collection_key=result.collection_key,
        collection_name=result.collection_name,
        collection_period=result.collection_period,
        fill_key=result.fill_key,
        fill_name=result.fill_name,
        fill_period=result.fill_period,
        fill_used=result.fill_used,
        nodata=result.nodata,
        clip_name=result.clip_name,
    )


def clear_cache(cache_dir: Optional[Path] = None) -> int:
    """Delete cached rasters and sidecars. Returns how many files went.

    The README stays. So does anything that isn't a cache file.
    """
    directory = Path(cache_dir) if cache_dir else default_cache_dir()
    if not directory.is_dir():
        return 0
    removed = 0
    for pattern in ("*.tif", "*.json", "*.json.tmp"):
        for path in directory.glob(pattern):
            path.unlink()
            removed += 1
    return removed
