"""Data Download routes.

The registry decides which sources exist. This module only
forwards a request into create_source / get_sites / get_data.
Include / exclude / search stay in the browser — they filter
a list that is already loaded.
"""

from datetime import date, datetime
from typing import Any, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from data_sources import SiteInfo, create_source, list_sources
from gui.shapefile_upload import bbox_from_shapefile_zip

from .errors import http_error
from .exports import (
    dataframe_to_csv_bytes,
    dataframe_to_sqlite_bytes,
    parts_to_zip,
    safe_stem,
    unique_stems,
)

router = APIRouter()

# A watershed zip is a few megabytes. Anything bigger is not
# one of those, and we stop reading once we pass the cap.
_MAX_SHAPEFILE_BYTES = 20 * 1024 * 1024
# Browser form posts from other websites can reach this route
# (CORS only hides the reply). These are the Vite dev origins.
_LOCAL_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
}


class SitesBody(BaseModel):
    state: Optional[str] = None
    site_ids: Optional[List[str]] = None
    # [west, south, east, north] — same order as get_sites bbox.
    bbox: Optional[List[float]] = None
    agency_code: str = "USGS"
    site_type_code: str = "ST"
    verify_ssl: bool = True


class ParametersBody(BaseModel):
    site_ids: List[str] = Field(default_factory=list)
    verify_ssl: bool = True


class SeriesSpec(BaseModel):
    """One catalogue row. Extra keys from the table are ignored."""

    model_config = ConfigDict(extra="ignore")

    site_id: str
    code: str = ""
    name: str = ""
    time_series_id: str = ""
    computation: str = ""
    dataset_identifier: str = ""
    duration: str = ""
    ordinal: Any = None
    height_depth: str = ""


class DownloadBody(BaseModel):
    start_date: date
    end_date: date
    series: List[SeriesSpec] = Field(default_factory=list)
    file_format: str = "csv"
    layout: str = "combined"
    verify_ssl: bool = True


def _option_dict(opt) -> dict:
    return {
        "key": opt.key,
        "label": opt.label,
        "kind": opt.kind,
        "default": opt.default,
        "help": opt.help,
        "choices": list(opt.choices),
    }


def _open_source(key: str, verify_ssl: bool):
    try:
        if key in ("aquarius", "snotel"):
            return create_source(key, verify_ssl=verify_ssl)
        return create_source(key)
    except ValueError as exc:
        raise http_error(exc) from exc


def _site_dict(site: SiteInfo) -> dict:
    meta = site.metadata or {}
    return {
        "site_id": site.site_id,
        "name": site.name,
        "latitude": site.latitude,
        "longitude": site.longitude,
        "state": site.state,
        "county": site.county,
        "elevation_ft": site.elevation_ft,
        "source": site.source,
        "agency_code": meta.get("agency_code") or "",
        "site_type": meta.get("site_type") or meta.get("site_type_code") or "",
    }


def _bbox_tuple(raw: Optional[List[float]]):
    if not raw:
        return None
    if len(raw) != 4:
        raise ValueError("bbox needs four numbers: west, south, east, north.")
    west, south, east, north = (float(v) for v in raw)
    if west >= east or south >= north:
        raise ValueError("bbox is empty or flipped (west < east, south < north).")
    return (west, south, east, north)


@router.get("")
def catalog() -> dict:
    """Source picker. Does not construct a source."""
    sources = []
    for desc in list_sources():
        sources.append({
            "key": desc.key,
            "display_name": desc.display_name,
            "description": desc.description,
            "options": [_option_dict(opt) for opt in desc.options],
        })
    return {"sources": sources}


@router.post("/shapefile-bbox")
async def shapefile_bbox(request: Request, file: UploadFile = File(...)) -> dict:
    """ZIP -> rectangle around the shapefile. Not point-in-polygon."""
    # No Origin header means a script or same-machine client, which
    # is fine. A browser on another site always sends one.
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin and origin not in _LOCAL_ORIGINS:
        raise HTTPException(
            status_code=403,
            detail="Shapefile uploads are only accepted from the local page.",
        )
    data = bytearray()
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        if len(data) + len(block) > _MAX_SHAPEFILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Shapefile ZIP is over the 20 MB limit.",
            )
        data.extend(block)
    try:
        bbox, note = bbox_from_shapefile_zip(bytes(data))
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise http_error(ValueError(str(exc))) from exc
    west, south, east, north = bbox
    return {
        "bbox": [west, south, east, north],
        "note": note,
    }


@router.post("/{key}/sites")
def sites(key: str, body: SitesBody) -> dict:
    state = (body.state or "").strip() or None
    ids = [s.strip() for s in (body.site_ids or []) if str(s).strip()]
    ids = ids or None
    try:
        bbox = _bbox_tuple(body.bbox)
    except ValueError as exc:
        raise http_error(exc) from exc

    # Same guards as the Streamlit discovery page.
    if key in ("usgs", "snotel") and bbox is None and not state and not ids:
        raise http_error(ValueError(
            "Give a state and/or site IDs first — an unfiltered "
            "pull is huge for this source. Or draw a box and "
            "use Find sites in box."
        ))
    if key == "snotel" and bbox is not None and not state:
        raise http_error(ValueError(
            "SNOTEL bbox search still needs a State (e.g. OR) "
            "so it doesn't pull every station nationwide first."
        ))

    kwargs = {}
    if key == "usgs":
        if body.agency_code.strip():
            kwargs["agency_code"] = body.agency_code.strip()
        if body.site_type_code.strip():
            kwargs["site_type_code"] = body.site_type_code.strip()

    source = _open_source(key, body.verify_ssl)
    try:
        found = source.get_sites(
            state=state,
            site_ids=ids,
            bbox=bbox,
            **kwargs,
        )
    except Exception as exc:
        raise http_error(exc) from exc

    rows = [_site_dict(site) for site in found]
    skipped = sum(
        1 for row in rows
        if row["latitude"] is None or row["longitude"] is None
    )
    return {"sites": rows, "count": len(rows), "skipped_no_coords": skipped}


@router.post("/{key}/parameters")
def parameters(key: str, body: ParametersBody) -> dict:
    ids = [s.strip() for s in body.site_ids if str(s).strip()]
    if not ids:
        raise http_error(ValueError("Pick at least one site first."))
    source = _open_source(key, body.verify_ssl)
    try:
        rows = source.get_available_parameters_for_sites(ids)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"parameters": rows, "count": len(rows)}


def _stem_for(row: SeriesSpec, source_key: str) -> str:
    site = row.site_id or "site"
    code = row.code or "param"
    if source_key == "usgs":
        tid = (row.time_series_id or "")[:8]
        return safe_stem(f"{site}_{code}_{row.computation}_{tid}")
    if source_key == "aquarius":
        ident = row.dataset_identifier or row.code or "dataset"
        return safe_stem(ident.replace("@", "_at_"))
    return safe_stem(
        f"{site}_{code}_{row.duration}_ord{row.ordinal}_{row.height_depth}"
    )


def _points_for_row(source, source_key: str, row: SeriesSpec, start: date, end: date):
    """Exact ids only — same rule as the Streamlit download page."""
    start_dt = datetime(start.year, start.month, start.day)
    end_dt = datetime(end.year, end.month, end.day)
    site_id = str(row.site_id)

    if source_key == "usgs":
        return source.get_data(
            site_ids=[site_id],
            parameter=str(row.code),
            start_date=start_dt,
            end_date=end_dt,
            time_series_id=str(row.time_series_id),
        )
    if source_key == "aquarius":
        identifier = str(row.dataset_identifier or row.code or "")
        return source.get_data(
            site_ids=[site_id],
            parameter=identifier,
            start_date=start_dt,
            end_date=end_dt,
            dataset_identifiers=[identifier],
        )
    duration = str(row.duration or "DAILY").upper()
    try:
        ordinal = int(float(row.ordinal if row.ordinal not in (None, "") else 1))
    except (TypeError, ValueError):
        ordinal = 1
    return source.get_data(
        site_ids=[site_id],
        parameter=str(row.code),
        start_date=start_dt,
        end_date=end_dt,
        duration=duration,
        ordinal=ordinal,
        height_depth=row.height_depth,
    )


@router.post("/{key}/download")
def download(key: str, body: DownloadBody) -> Response:
    if body.end_date < body.start_date:
        raise http_error(ValueError("End date is before start date."))
    if not body.series:
        raise http_error(ValueError("Pick at least one series."))
    fmt = body.file_format.lower().strip()
    if fmt not in ("csv", "sqlite"):
        raise http_error(ValueError("file_format must be csv or sqlite."))
    layout = body.layout.lower().strip()
    if layout not in ("combined", "per_series"):
        raise http_error(ValueError("layout must be combined or per_series."))

    source = _open_source(key, body.verify_ssl)
    parts = []
    errors = []
    all_points = []
    for row in body.series:
        label = row.name or row.code or row.site_id
        try:
            points = _points_for_row(
                source, key, row, body.start_date, body.end_date
            )
            part_df = source.to_dataframe(points)
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        parts.append({"stem": _stem_for(row, key), "df": part_df})
        all_points.extend(points)

    if not parts:
        raise http_error(ValueError(
            "Every series failed.\n" + "\n".join(errors)
        ))
    if not all_points:
        raise http_error(ValueError(
            "Download returned no points for that range."
            + ("\n" + "\n".join(errors) if errors else "")
        ))

    stems = unique_stems([part["stem"] for part in parts])
    for part, stem in zip(parts, stems):
        part["stem"] = stem

    base = f"{key}_{body.start_date}_{body.end_date}"
    if layout == "per_series":
        payload = parts_to_zip(parts, "csv" if fmt == "csv" else "db")
        filename = f"{base}.zip"
        media = "application/zip"
    elif fmt == "csv":
        payload = dataframe_to_csv_bytes(source.to_dataframe(all_points))
        filename = f"{base}.csv"
        media = "text/csv"
    else:
        payload = dataframe_to_sqlite_bytes(source.to_dataframe(all_points))
        filename = f"{base}.db"
        media = "application/octet-stream"

    # quote() keeps unicode out of the header. The page decodes it.
    warning_text = "\n".join(errors)
    if len(warning_text) > 1500:
        warning_text = warning_text[:1500] + "\n…"
    warning = quote(warning_text) if warning_text else ""
    return Response(
        content=payload,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Point-Count": str(len(all_points)),
            "X-Series-Count": str(len(parts)),
            "X-Partial-Errors": warning,
        },
    )
