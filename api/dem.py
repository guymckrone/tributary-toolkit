"""DEM routes.

dem/ owns the grid, the catalog, the cache, and the GeoTIFF.
This file only turns JSON into a DemRequest and sends back
JSON or the file bytes.

A box drawn on the map is bbox. An uploaded shapefile,
GeoJSON, KML, or KMZ goes through /boundary first. The
download then gets that footprint as clip, and pixels
outside it become nodata.
"""

from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from dem.aoi import validate_bbox
from dem.boundary import Boundary, footprint_bounds, read_boundary
from dem.models import DemCollection, DemEstimate, DemRequest, DemResult
from dem.registry import create_provider, list_providers
from dem.service import DEFAULT_MAX_MB
from dem.service import estimate as estimate_dem
from dem.service import get_dem, list_collections

from .errors import http_error
from .exports import safe_stem

router = APIRouter()

# Same cap as the site-search shapefile upload. A watershed
# zip is a few megabytes. Anything bigger is not one of those.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# Browser form posts from other websites can reach this route
# (CORS only hides the reply). These are the Vite dev origins.
_LOCAL_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
}

# The download body is the GeoTIFF, so the facts that are not
# inside the file ride in headers. Text ones are percent-encoded
# (a comma or a non-ascii name would otherwise break the header).
# The page decodes them, same as X-Partial-Errors.
DEM_RESPONSE_HEADERS = (
    "X-Dem-Provider",
    "X-Dem-Requested-Resolution",
    "X-Dem-Source-Resolution",
    "X-Dem-Coarsest-Resolution",
    "X-Dem-Epsg",
    "X-Dem-Bbox-Projected",
    "X-Dem-Vertical-Unit",
    "X-Dem-From-Cache",
    "X-Dem-Collection-Key",
    "X-Dem-Collection-Name",
    "X-Dem-Collection-Period",
    "X-Dem-Fill-Key",
    "X-Dem-Fill-Name",
    "X-Dem-Fill-Period",
    "X-Dem-Fill-Used",
    "X-Dem-Nodata",
    "X-Dem-Clip-Name",
    "X-Dem-Description",
    "X-Dem-Zero-Warning",
)


class CollectionsBody(BaseModel):
    """bbox is [west, south, east, north], same order as site search."""

    bbox: List[float]
    provider: str = "3dep"
    # Work network: send false. Default stays true so a call from
    # off that network still checks the certificate.
    verify_ssl: bool = True


class EstimateBody(BaseModel):
    bbox: List[float]
    # Meters, or "best" (finest cell, or the locked collection's own).
    resolution_m: Union[float, str] = "best"
    provider: str = "3dep"
    max_mb: float = DEFAULT_MAX_MB
    verify_ssl: bool = True
    collection_key: Optional[str] = None
    fill_key: Optional[str] = None


class ClipBody(BaseModel):
    """The /boundary response, minus bbox. bbox is the request box."""

    name: str = ""
    footprint: dict


class DownloadBody(EstimateBody):
    clip: Optional[ClipBody] = None


def _blank(value: Optional[str]) -> Optional[str]:
    """A missing or whitespace key means the unlocked mosaic."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _provider_key(value: Optional[str]) -> str:
    return (value or "").strip() or "3dep"


def _dem_request(body: EstimateBody) -> DemRequest:
    return DemRequest(
        bbox=validate_bbox(body.bbox),
        resolution_m=body.resolution_m,
        provider=_provider_key(body.provider),
    )


def _header_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > 1500:
        text = text[:1500] + "…"
    return quote(text)


def _header_num(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def _collection_dict(item: DemCollection) -> dict:
    return {
        "key": item.key,
        "name": item.name,
        "kind": item.kind,
        "resolution_m": item.resolution_m,
        "year": item.year,
        "start_text": item.start_text,
        "end_text": item.end_text,
        "acquisition_year": item.acquisition_year,
        "period_label": item.period_label(),
        "coverage_pct": item.coverage_pct,
        "covers_all": item.covers_all,
        "vertical_datum": item.vertical_datum,
        "dem_type": item.dem_type,
        "tile_count": item.tile_count,
        "tile_names": list(item.tile_names),
        "footprint": item.footprint,
    }


def _estimate_dict(estimated: DemEstimate) -> dict:
    return {
        "provider": estimated.provider,
        "resolution_m": estimated.resolution_m,
        "width_px": estimated.width_px,
        "height_px": estimated.height_px,
        "estimated_mb": estimated.estimated_mb,
        "n_requests": estimated.n_requests,
        "over_cap": estimated.over_cap,
        "source_resolution_m": estimated.source_resolution_m,
        "coarsest_resolution_m": estimated.coarsest_resolution_m,
    }


def _download_name(result: DemResult) -> str:
    """Ascii only. The name sits in a header, and the tiff is already written.

    A non-ascii clip name must not fail the response after the
    download succeeded. I drop those characters.
    """
    parts = [
        "dem",
        result.provider or "dem",
        f"{result.requested_resolution_m:g}m",
    ]
    if result.collection_key:
        parts.append(result.collection_key)
    if result.clip_name:
        parts.append(Path(result.clip_name).stem)
    stem = safe_stem("_".join(parts))
    ascii_stem = stem.encode("ascii", "ignore").decode("ascii").strip("._")
    return f"{ascii_stem or 'dem'}.tif"


def _dem_headers(result: DemResult) -> dict:
    xmin, ymin, xmax, ymax = result.bbox_projected
    return {
        "Content-Disposition": (
            f'attachment; filename="{_download_name(result)}"'
        ),
        "X-Dem-Provider": _header_text(result.provider),
        "X-Dem-Requested-Resolution": _header_num(result.requested_resolution_m),
        "X-Dem-Source-Resolution": _header_num(result.source_resolution_m),
        "X-Dem-Coarsest-Resolution": _header_num(result.coarsest_resolution_m),
        "X-Dem-Epsg": str(result.epsg),
        "X-Dem-Bbox-Projected": f"{xmin:.3f},{ymin:.3f},{xmax:.3f},{ymax:.3f}",
        "X-Dem-Vertical-Unit": _header_text(result.vertical_unit),
        "X-Dem-From-Cache": "true" if result.from_cache else "false",
        "X-Dem-Collection-Key": _header_text(result.collection_key),
        "X-Dem-Collection-Name": _header_text(result.collection_name),
        "X-Dem-Collection-Period": _header_text(result.collection_period),
        "X-Dem-Fill-Key": _header_text(result.fill_key),
        "X-Dem-Fill-Name": _header_text(result.fill_name),
        "X-Dem-Fill-Period": _header_text(result.fill_period),
        "X-Dem-Fill-Used": "true" if result.fill_used else "false",
        "X-Dem-Nodata": _header_num(result.nodata),
        "X-Dem-Clip-Name": _header_text(result.clip_name),
        "X-Dem-Description": _header_text(result.source_description),
        "X-Dem-Zero-Warning": _header_text(result.zero_warning),
    }


def _clip_from_body(clip: Optional[ClipBody]):
    if clip is None:
        return None
    # A file input can hand me "C:\fakepath\basin.geojson".
    # Only the last piece is the name. I split on both
    # separators so this does not depend on the server OS.
    raw_name = (clip.name or "").replace("\\", "/").split("/")[-1].strip()
    name = raw_name or "upload"
    return Boundary(
        name=name,
        bbox=footprint_bounds(clip.footprint),
        footprint=clip.footprint,
    )


@router.get("/providers", name="dem_providers")
def providers() -> dict:
    """Picker catalog. Does not construct a provider or hit the network."""
    rows = []
    for desc in list_providers():
        rows.append({
            "key": desc.key,
            "display_name": desc.display_name,
            "description": desc.description,
        })
    return {"providers": rows}


@router.post("/collections", name="dem_collections")
def collections(body: CollectionsBody) -> dict:
    """Datasets that overlap the box. No raster is downloaded."""
    try:
        bbox = validate_bbox(body.bbox)
        # list_collections builds 3DEP when I pass it nothing.
        # I build the one named in the body so a later provider
        # is a registry entry, not a new URL. Construction itself
        # does not hit the network.
        provider = create_provider(
            _provider_key(body.provider),
            verify_ssl=body.verify_ssl,
        )
        catalog = list_collections(bbox, provider=provider)
    except Exception as exc:
        raise http_error(exc) from exc
    return {
        "collections": [_collection_dict(item) for item in catalog.collections],
        "suggested_key": catalog.suggested_key,
        "coverage_spacing_m": catalog.coverage_spacing_m,
    }


@router.post("/estimate", name="dem_estimate")
def estimate(body: EstimateBody) -> dict:
    """Size of the rectangle. over_cap is a flag, not an error.

    collection_key changes "best" to that collection's own cell
    size, so the pixel count can differ from the unlocked mosaic.
    A fill does not change the size. A clip does not either:
    the corners become nodata, but they are still pixels.
    """
    try:
        estimated = estimate_dem(
            _dem_request(body),
            max_mb=body.max_mb,
            verify_ssl=body.verify_ssl,
            collection_key=_blank(body.collection_key),
            fill_key=_blank(body.fill_key),
        )
    except Exception as exc:
        raise http_error(exc) from exc
    return _estimate_dict(estimated)


async def _read_upload(file: UploadFile) -> bytes:
    data = bytearray()
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        if len(data) + len(block) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Upload is over the 20 MB limit.",
            )
        data.extend(block)
    return bytes(data)


@router.post("/boundary", name="dem_boundary")
async def boundary(request: Request, file: UploadFile = File(...)) -> dict:
    """File -> name, rectangle, and polygons. Nothing is downloaded.

    The download still asks the elevation server for the rectangle.
    Pass footprint back as clip to punch out the corners.
    """
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin and origin not in _LOCAL_ORIGINS:
        raise HTTPException(
            status_code=403,
            detail="Uploads are only accepted from the local page.",
        )
    data = await _read_upload(file)
    try:
        found = read_boundary(file.filename or "", data)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise http_error(ValueError(str(exc))) from exc
    west, south, east, north = found.bbox
    return {
        "name": found.name,
        "bbox": [west, south, east, north],
        "footprint": found.footprint,
    }


@router.post("/download", name="dem_download")
def download(body: DownloadBody) -> Response:
    """GeoTIFF bytes. Metadata is in the X-Dem-* headers.

    Over the cap, an unknown collection, or an outline that
    misses the box is a 400. The service writes the file into
    the cache; I read those bytes and leave the cache alone,
    so the same request again is a hit.
    """
    try:
        clip = _clip_from_body(body.clip)
        result = get_dem(
            _dem_request(body),
            max_mb=body.max_mb,
            verify_ssl=body.verify_ssl,
            collection_key=_blank(body.collection_key),
            fill_key=_blank(body.fill_key),
            clip=clip,
        )
        payload = Path(result.path).read_bytes()
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from exc
    return Response(
        content=payload,
        media_type="image/tiff",
        headers=_dem_headers(result),
    )
