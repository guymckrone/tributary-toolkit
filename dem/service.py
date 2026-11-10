"""The DEM entry points callers should use.

Providers know how to talk to one server. This module decides
whether to download at all: cache hit, size cap, then fetch.
"""

from dataclasses import replace
from pathlib import Path
from typing import Optional

from .boundary import Boundary, footprint_bounds
from .cache import cache_key, default_cache_dir, lookup, store
from .composite import NODATA, describe_lock, punch_outside_file
from .models import DemEstimate, DemRequest, DemResult
from .registry import create_provider, list_providers

# Uncompressed Float32. Chosen to sit just under one max-size
# ImageServer request (8000 x 8000 px is 256 MB). Raise it
# per call when a bigger box is deliberate.
DEFAULT_MAX_MB = 250


def estimate(
    request: DemRequest,
    max_mb: float = DEFAULT_MAX_MB,
    provider=None,
    timeout: int = 90,
    verify_ssl: bool = True,
    collection_key: Optional[str] = None,
    fill_key: Optional[str] = None,
) -> DemEstimate:
    """How big the download would be. Does not fetch or read the cache.

    over_cap is True when the uncompressed size is over max_mb.
    This does not raise. get_dem() raises. A UI can show the
    estimate and let the user raise the cap.

    With collection_key, the size is the locked grid. "best" then
    means that collection's own cell size, not the finest thing
    anywhere in the box. A fill does not change the size. The cap
    is one output grid, not one grid per source.
    """
    if max_mb <= 0:
        raise ValueError(f"max_mb must be > 0, got {max_mb}")
    if provider is None:
        provider = create_provider(
            request.provider, timeout=timeout, verify_ssl=verify_ssl
        )
    if collection_key is None and fill_key is None:
        estimated = provider.estimate(request)
    else:
        primary, _fill, download_fill, resolution = _prepare_lock(
            provider, request, collection_key, fill_key
        )
        estimated = provider.locked_estimate(
            request, resolution, primary, download_fill
        )
    return replace(estimated, over_cap=estimated.estimated_mb > max_mb)


def _cap_message(estimated: DemEstimate, max_mb: float) -> str:
    source = estimated.source_resolution_m
    source_text = f"{source:g} m" if source is not None else "unknown"
    return (
        f"DEM is {estimated.width_px} x {estimated.height_px} pixels "
        f"at {estimated.resolution_m:g} m "
        f"({estimated.estimated_mb:.1f} MB uncompressed), over the "
        f"{max_mb:g} MB cap. Raise max_mb to download it, or use a "
        f"smaller box or a coarser resolution. "
        f"Source resolution is {source_text}."
    )


def get_dem(
    request: DemRequest,
    max_mb: float = DEFAULT_MAX_MB,
    cache_dir: Optional[Path] = None,
    provider=None,
    timeout: int = 90,
    verify_ssl: bool = True,
    collection_key: Optional[str] = None,
    fill_key: Optional[str] = None,
    clip=None,
) -> DemResult:
    """A GeoTIFF for this request, from the cache when we already have it.

    Order: cache, then estimate, then the cap, then fetch. A hit
    does not check the cap again. The cap is about starting a
    download, and the file is already on disk.

    Leave collection_key empty and this is the server's own mosaic,
    same as before. Pass a key from list_collections and the raster
    is that dataset only. Pixels outside its footprint are nodata
    (-999999), not some other year. fill_key is a second dataset
    used only in those holes. Where the primary has data, the fill
    is thrown away.

    If the primary already covers the box I skip the fill download.
    The footprint mask can still punch a sliver the coverage sample
    missed. Both names and years are recorded either way.

    A side longer than one ImageServer request is downloaded in
    pieces and stitched. Every piece uses the same collection
    and the same fill. The size cap is the whole stitched grid.
    Over max_mb raises here, with the estimate in the message,
    and nothing is written.

    clip is a Boundary from read_boundary(), or a GeoJSON polygon.
    The server still gets request.bbox, which should be that
    outline's rectangle. Pixels outside the polygons are nodata.
    A box drawn on the map is just request.bbox, and clip stays empty.
    """
    footprint, clip_name, token = _clip_parts(clip)
    directory = Path(cache_dir) if cache_dir else default_cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    cached = lookup(request, directory, collection_key, fill_key, token)
    if cached is not None:
        return cached
    if footprint is not None:
        _check_clip_overlaps(request, footprint)

    if provider is None:
        provider = create_provider(
            request.provider, timeout=timeout, verify_ssl=verify_ssl
        )
    if collection_key is None and fill_key is None:
        estimated = estimate(request, max_mb=max_mb, provider=provider)
        if estimated.over_cap:
            raise ValueError(_cap_message(estimated, max_mb))
        tif_path = directory / f"{cache_key(request, clip_token=token)}.tif"
        fetched = provider.fetch(request, str(tif_path))
        return _finish(
            request, fetched, directory, None, None, token,
            footprint, clip_name,
        )

    primary, fill, download_fill, resolution = _prepare_lock(
        provider, request, collection_key, fill_key
    )
    estimated = provider.locked_estimate(
        request, resolution, primary, download_fill
    )
    if estimated.estimated_mb > max_mb:
        raise ValueError(_cap_message(estimated, max_mb))

    tif_path = directory / (
        f"{cache_key(request, collection_key, fill_key, token)}.tif"
    )
    fetched = provider.fetch_locked(
        request, str(tif_path), primary, download_fill, resolution
    )
    # The provider only knows what it downloaded. If I skipped the
    # fill, the name still belongs on the result.
    recorded_fill = download_fill if download_fill is not None else fill
    description = describe_lock(
        primary, recorded_fill, download_fill is not None
    )
    per_tile = 2 if download_fill is not None else 1
    tile_count = estimated.n_requests // per_tile
    if tile_count > 1:
        description += (
            f" Stitched from {tile_count} requests, each locked "
            "to the same collection."
        )
    fetched = replace(
        fetched,
        source_description=description,
        fill_key=None if recorded_fill is None else recorded_fill.key,
        fill_name=None if recorded_fill is None else recorded_fill.name,
        fill_period=(
            None if recorded_fill is None else recorded_fill.period_label()
        ),
        fill_used=download_fill is not None,
    )
    return _finish(
        request, fetched, directory, collection_key, fill_key, token,
        footprint, clip_name,
    )


def _clip_parts(clip):
    """Footprint, display name, and cache token. All None when unclipped."""
    if clip is None:
        return None, None, None
    if isinstance(clip, Boundary):
        return clip.footprint, clip.name, clip.cache_token()
    if isinstance(clip, dict):
        # Same rounding as Boundary.cache_token, so a file and the
        # GeoJSON it produced share a cache entry.
        dummy = Boundary(name="", bbox=footprint_bounds(clip), footprint=clip)
        return clip, None, dummy.cache_token()
    raise ValueError(
        "clip must be a Boundary from read_boundary(), or GeoJSON."
    )


def _check_clip_overlaps(request, footprint) -> None:
    outline = footprint_bounds(footprint)
    west, south, east, north = request.bbox
    if outline[2] <= west or outline[0] >= east or outline[3] <= south or outline[1] >= north:
        raise ValueError(
            "The outline does not overlap this box. "
            "Use the outline's own rectangle as the request bbox."
        )


def _finish(
    request,
    fetched,
    directory,
    collection_key,
    fill_key,
    clip_token,
    footprint,
    clip_name,
):
    if footprint is not None:
        xmin, ymin, xmax, ymax = fetched.bbox_projected
        punch_outside_file(
            fetched.path,
            footprint,
            fetched.epsg,
            xmin,
            ymax,
            fetched.requested_resolution_m,
        )
        label = clip_name or "the uploaded outline"
        fetched = replace(
            fetched,
            source_description=(
                f"{fetched.source_description} "
                f"Cells outside {label} are nodata ({NODATA:.0f})."
            ).strip(),
            nodata=NODATA,
            clip_name=clip_name,
        )
    return store(
        request, fetched, directory, collection_key, fill_key, clip_token
    )


def _prepare_lock(provider, request, collection_key, fill_key):
    """The primary, the named fill, the fill to actually download, the cell size.

    download_fill is None when there is no fill, or when the
    primary already covers the box and the extra request would
    only be thrown away.
    """
    if fill_key and not collection_key:
        raise ValueError(
            "fill_key needs a collection_key. The fill is only used in holes."
        )
    if collection_key is not None and not str(collection_key).strip():
        raise ValueError("collection_key is empty.")
    if fill_key is not None and not str(fill_key).strip():
        raise ValueError("fill_key is empty.")
    catalog = provider.list_collections(request.bbox)
    primary = _find_collection(catalog, collection_key, "collection")
    fill = None
    if fill_key:
        if fill_key == collection_key:
            raise ValueError(
                "collection_key and fill_key are the same dataset. "
                "The fill has to be a different one."
            )
        fill = _find_collection(catalog, fill_key, "fill")
    if primary.footprint is None:
        raise ValueError(
            f"{primary.name} has no footprint, so I can't tell its "
            "ground from a hole. Pick a different collection."
        )
    if fill is not None and fill.footprint is None:
        raise ValueError(
            f"{fill.name} has no footprint, so I can't tell where it "
            "is allowed to fill holes."
        )
    if request.resolution_m == "best":
        resolution = float(primary.resolution_m)
    else:
        resolution = float(request.resolution_m)
    download_fill = None if fill is None or primary.covers_all else fill
    return primary, fill, download_fill, resolution


def _find_collection(catalog, key, role):
    for item in catalog.collections:
        if item.key == key:
            return item
    known = ", ".join(item.key for item in catalog.collections) or "none"
    raise ValueError(
        f"No {role} {key!r} overlaps this box. "
        f"Keys from list_collections: {known}."
    )


def list_collections(
    bbox,
    provider=None,
    timeout: int = 90,
    verify_ssl: bool = True,
):
    """What datasets overlap this box, grouped for the picker.

    No raster is downloaded. Pass the provider you already have
    (the smoke test does) so verify_ssl stays consistent.
    """
    if provider is None:
        provider = create_provider(
            "3dep", timeout=timeout, verify_ssl=verify_ssl
        )
    return provider.list_collections(bbox)


# Re-exported so a caller can do `from dem import list_providers`
# and also clear the cache without importing dem.cache.
__all__ = [
    "DEFAULT_MAX_MB",
    "estimate",
    "get_dem",
    "list_collections",
    "list_providers",
]
