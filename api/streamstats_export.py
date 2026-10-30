"""Watershed downloads: GeoJSON, shapefile, KML, KMZ.

One basin comes back as one file. A shapefile is still a zip,
because it is really four files (.shp .shx .dbf .prj).
Two or more basins come back as a zip of separate files.
I don't merge them into one layer — the owner wants one
file per watershed.
"""

import csv
import io
import json
import zipfile
from typing import Any, Dict, List
from xml.etree import ElementTree as ET

import shapefile
from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .errors import http_error
from .exports import safe_stem, unique_stems

router = APIRouter()

# WGS84. QGIS and Google Earth both read this .prj text.
_WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]]'
)

_KML_NS = "http://www.opengis.net/kml/2.2"
_FORMATS = ("geojson", "shp", "kml", "kmz")


class ExportBasin(BaseModel):
    id: str = "watershed"
    watershed: Dict[str, Any] = Field(default_factory=dict)
    characteristics: List[Dict[str, Any]] = Field(default_factory=list)
    flow_statistics: List[Dict[str, Any]] = Field(default_factory=list)


class ExportBody(BaseModel):
    format: str
    basins: List[ExportBasin]


def _attrs(item: ExportBasin) -> Dict[str, Any]:
    """The columns I put on every format. Same names, DBF-safe."""
    ws = item.watershed or {}
    snap = ws.get("snap") or {}
    warning = (ws.get("warning") or snap.get("warning") or "")
    drnarea = None
    for row in item.characteristics or []:
        if str(row.get("code") or "").upper() == "DRNAREA":
            drnarea = row.get("value")
            break
    return {
        "id": item.id or "watershed",
        "snap_lat": snap.get("latitude"),
        "snap_lon": snap.get("longitude"),
        "region": ws.get("region") or snap.get("region") or "",
        "huc_id": snap.get("huc_id") or "",
        "shape_area": ws.get("shape_area"),
        "drnarea": drnarea,
        "warning": str(warning)[:254],
    }


def _geometry(item: ExportBasin) -> Dict[str, Any]:
    feature = (item.watershed or {}).get("basin_geojson") or {}
    geometry = feature.get("geometry") or {}
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype not in ("Polygon", "MultiPolygon") or not coords:
        raise ValueError(
            f"{item.id!r} has no polygon. "
            "I only export Polygon and MultiPolygon basins."
        )
    return geometry


def _polygons(geometry: Dict[str, Any]) -> List[list]:
    """Each entry is one polygon: [outer ring, hole, hole, ...]."""
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    return list(geometry["coordinates"])


def _closed(ring: list) -> list:
    if len(ring) >= 2 and ring[0] != ring[-1]:
        return list(ring) + [ring[0]]
    return list(ring)


def _signed_area(ring: list) -> float:
    """Positive when the ring is clockwise, with this sum."""
    total = 0.0
    pts = _closed(ring)
    for i in range(len(pts) - 1):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[i + 1][0], pts[i + 1][1]
        total += (x2 - x1) * (y2 + y1)
    return total


def _as_clockwise(ring: list, clockwise: bool) -> list:
    """Shapefiles want the outer ring clockwise, holes the other way.

    GeoJSON outers are counter-clockwise, so I flip those.
    """
    pts = _closed(ring)
    is_clockwise = _signed_area(pts) > 0
    if is_clockwise != clockwise:
        pts = list(reversed(pts))
    return [[float(p[0]), float(p[1])] for p in pts]


def basin_to_geojson(item: ExportBasin) -> bytes:
    """One Feature. I keep USGS properties and add my columns."""
    feature = (item.watershed or {}).get("basin_geojson") or {}
    geometry = _geometry(item)
    props = dict(feature.get("properties") or {})
    props.update(_attrs(item))
    out = {
        "type": "Feature",
        "geometry": geometry,
        "properties": props,
    }
    return json.dumps(out).encode("utf-8")


def basin_to_shapefile_files(item: ExportBasin, stem: str) -> Dict[str, bytes]:
    """Four files in memory. stem is the filename without an extension."""
    geometry = _geometry(item)
    attrs = _attrs(item)
    parts = []
    for polygon in _polygons(geometry):
        if not polygon:
            continue
        # Ring 0 is the outside. The rest are holes.
        for i, ring in enumerate(polygon):
            if len(ring) < 3:
                continue
            parts.append(_as_clockwise(ring, clockwise=(i == 0)))
    if not parts:
        raise ValueError(f"{item.id!r} polygon has no rings.")

    shp_io, shx_io, dbf_io = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(
        shp=shp_io,
        shx=shx_io,
        dbf=dbf_io,
        shapeType=shapefile.POLYGON,
    ) as writer:
        # DBF names are capped at 10 characters. Sizes are the
        # old dBase limits (text max 254).
        writer.field("id", "C", size=80)
        writer.field("snap_lat", "F", size=19, decimal=6)
        writer.field("snap_lon", "F", size=19, decimal=6)
        writer.field("region", "C", size=8)
        writer.field("huc_id", "C", size=20)
        writer.field("shape_area", "F", size=19, decimal=4)
        writer.field("drnarea", "F", size=19, decimal=4)
        writer.field("warning", "C", size=254)
        writer.poly(parts)
        writer.record(
            attrs["id"][:80],
            attrs["snap_lat"],
            attrs["snap_lon"],
            str(attrs["region"])[:8],
            str(attrs["huc_id"])[:20],
            attrs["shape_area"],
            attrs["drnarea"],
            attrs["warning"],
        )
    return {
        f"{stem}.shp": shp_io.getvalue(),
        f"{stem}.shx": shx_io.getvalue(),
        f"{stem}.dbf": dbf_io.getvalue(),
        f"{stem}.prj": _WGS84_PRJ.encode("ascii"),
    }


def _kml_tag(parent, name: str) -> ET.Element:
    return ET.SubElement(parent, f"{{{_KML_NS}}}{name}")


def _add_data(extended, name: str, value: Any) -> None:
    data = _kml_tag(extended, "Data")
    data.set("name", name)
    _kml_tag(data, "value").text = "" if value is None else str(value)


def _add_polygon(parent, rings: list) -> None:
    polygon = _kml_tag(parent, "Polygon")
    for i, ring in enumerate(rings):
        if len(ring) < 3:
            continue
        boundary = _kml_tag(
            polygon,
            "outerBoundaryIs" if i == 0 else "innerBoundaryIs",
        )
        linear = _kml_tag(boundary, "LinearRing")
        text = " ".join(
            f"{float(p[0])},{float(p[1])},0" for p in _closed(ring)
        )
        _kml_tag(linear, "coordinates").text = text


def basin_to_kml(item: ExportBasin) -> str:
    """One basin polygon, plus a point placemark at the snap."""
    geometry = _geometry(item)
    attrs = _attrs(item)
    ET.register_namespace("", _KML_NS)
    kml = ET.Element(f"{{{_KML_NS}}}kml")
    doc = _kml_tag(kml, "Document")
    _kml_tag(doc, "name").text = str(attrs["id"])

    placemark = _kml_tag(doc, "Placemark")
    _kml_tag(placemark, "name").text = str(attrs["id"])
    extended = _kml_tag(placemark, "ExtendedData")
    for key, value in attrs.items():
        _add_data(extended, key, value)

    polygons = [p for p in _polygons(geometry) if p]
    if len(polygons) == 1:
        _add_polygon(placemark, polygons[0])
    else:
        multi = _kml_tag(placemark, "MultiGeometry")
        for rings in polygons:
            _add_polygon(multi, rings)

    if attrs["snap_lat"] is not None and attrs["snap_lon"] is not None:
        point_mark = _kml_tag(doc, "Placemark")
        _kml_tag(point_mark, "name").text = f"{attrs['id']} snap"
        point = _kml_tag(point_mark, "Point")
        _kml_tag(point, "coordinates").text = (
            f"{float(attrs['snap_lon'])},{float(attrs['snap_lat'])},0"
        )

    return ET.tostring(kml, encoding="unicode", xml_declaration=True)


def _characteristics_csv(items: List[ExportBasin]) -> bytes:
    """Long table: one row per basin and characteristic. Empty if none."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "code", "name", "value", "unit"])
    wrote = False
    for item in items:
        for row in item.characteristics or []:
            wrote = True
            value = row.get("value")
            writer.writerow([
                item.id or "watershed",
                row.get("code") or "",
                row.get("name") or "",
                "" if value is None else value,
                row.get("unit") or "",
            ])
    if not wrote:
        return b""
    return buf.getvalue().encode("utf-8")


def _flow_csv(items: List[ExportBasin]) -> bytes:
    """One row per basin and regression estimate. Empty if none."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "group", "code", "name", "value", "unit",
        "regression_region", "errors_or_limits", "equation",
    ])
    wrote = False
    for item in items:
        for row in item.flow_statistics or []:
            wrote = True
            value = row.get("value")
            writer.writerow([
                item.id or "watershed",
                row.get("group") or "",
                row.get("code") or "",
                row.get("name") or "",
                "" if value is None else value,
                row.get("unit") or "",
                row.get("regression_region") or "",
                row.get("errors_or_limits") or "",
                row.get("equation") or "",
            ])
    if not wrote:
        return b""
    return buf.getvalue().encode("utf-8")


def _zip_bytes(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _files_for(item: ExportBasin, stem: str, fmt: str) -> Dict[str, bytes]:
    if fmt == "geojson":
        return {f"{stem}.geojson": basin_to_geojson(item)}
    if fmt == "shp":
        return basin_to_shapefile_files(item, stem)
    if fmt == "kml":
        return {f"{stem}.kml": basin_to_kml(item).encode("utf-8")}
    if fmt == "kmz":
        # A KMZ is a zip whose main file is named doc.kml.
        kmz = _zip_bytes({"doc.kml": basin_to_kml(item).encode("utf-8")})
        return {f"{stem}.kmz": kmz}
    raise ValueError(f"Unknown format {fmt!r}. Use geojson, shp, kml, or kmz.")


def build_export(fmt: str, basins: List[ExportBasin]) -> tuple:
    """Return (filename, media_type, bytes)."""
    fmt = (fmt or "").strip().lower()
    if fmt not in _FORMATS:
        raise ValueError(
            f"Unknown format {fmt!r}. Use geojson, shp, kml, or kmz."
        )
    usable = []
    for item in basins:
        try:
            _geometry(item)
        except ValueError:
            # A failed delineation has no polygon. Leave it out.
            continue
        usable.append(item)
    if not usable:
        raise ValueError("No watershed polygons to export.")

    stems = unique_stems([safe_stem(item.id or "watershed") for item in usable])
    files: Dict[str, bytes] = {}
    for item, stem in zip(usable, stems):
        files.update(_files_for(item, stem, fmt))

    table = _characteristics_csv(usable)
    flows = _flow_csv(usable)
    extras: Dict[str, bytes] = {}
    if table:
        extras["basin_characteristics.csv"] = table
    if flows:
        extras["flow_statistics.csv"] = flows
    # A lone geojson/kml/kmz becomes a zip once a table rides along,
    # so the browser saves one download.
    if extras and len(usable) == 1 and fmt != "shp":
        files.update(extras)
        stem = stems[0]
        return f"{stem}_{fmt}.zip", "application/zip", _zip_bytes(files)
    files.update(extras)

    if len(usable) == 1 and fmt != "shp":
        name, payload = next(iter(files.items()))
        media = {
            "geojson": "application/geo+json",
            "kml": "application/vnd.google-earth.kml+xml",
            "kmz": "application/vnd.google-earth.kmz",
        }[fmt]
        return name, media, payload

    if len(usable) == 1 and fmt == "shp":
        stem = stems[0]
        return f"{stem}_shp.zip", "application/zip", _zip_bytes(files)

    return f"watersheds_{fmt}.zip", "application/zip", _zip_bytes(files)


@router.post("/export")
def export_watersheds(body: ExportBody) -> Response:
    try:
        filename, media, payload = build_export(body.format, body.basins)
    except Exception as exc:
        raise http_error(exc) from exc
    return Response(
        content=payload,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
