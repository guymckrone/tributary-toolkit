"""An area of interest from a file, not only a drawn rectangle.

The elevation server only accepts a rectangle. A watershed
shapefile is not a rectangle. I read the polygons, download the
rectangle around them, and the caller punches out every pixel
whose center falls outside the polygons.

Lon/lat only. A State Plane or UTM file is rejected, same as
the site-search shapefile reader. Reproject to WGS84 first.

Accepted files: a shapefile zip (.shp plus sidecars), a bare
.shp, GeoJSON (.json or .geojson), KML, and KMZ.
"""

import hashlib
import io
import json
import shutil
import struct
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .aoi import validate_bbox
from .models import BBox


@dataclass(frozen=True)
class Boundary:
    """Polygons from one upload, plus the rectangle around them."""

    name: str
    bbox: BBox
    # GeoJSON MultiPolygon in lon/lat. The same shape the
    # collection footprint uses, so the same mask code clips it.
    footprint: dict

    def cache_token(self) -> str:
        """Short id of this outline. Same shape, same token.

        The rectangle is not enough: two watersheds can share a
        bounding box and still punch different corners.
        """
        text = json.dumps(
            _round_footprint(self.footprint),
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read_boundary(filename: str, data: bytes) -> Boundary:
    """Polygons from an uploaded file.

    filename is the name the user picked, so the suffix decides
    the format. data is the raw bytes. Nothing is downloaded.
    """
    if not data:
        raise ValueError("Uploaded file is empty.")
    name = Path(filename or "").name or "upload"
    suffix = Path(name).suffix.lower()
    if suffix == ".kmz" or (suffix == ".zip" and _zip_has_kml(data) and not _zip_has_shp(data)):
        polygons = _polygons_from_kml(_kml_text_from_zip(data))
    elif suffix == ".zip":
        polygons, name = _polygons_from_shapefile_zip(data, name)
    elif suffix == ".shp":
        polygons = _polygons_from_shp(data)
    elif suffix == ".kml":
        polygons = _polygons_from_kml(data.decode("utf-8", errors="replace"))
    elif suffix in (".json", ".geojson"):
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError(f"{name} is not JSON.") from exc
        polygons = _polygons_from_geojson(payload)
    else:
        raise ValueError(
            f"Don't know how to read {name}. Use a shapefile zip, "
            "GeoJSON, KML, or KMZ."
        )
    return _boundary_from_polygons(name, polygons)


def footprint_bounds(footprint: dict) -> BBox:
    """(west, south, east, north) of a GeoJSON polygon."""
    points = list(_iter_lonlat(footprint))
    if not points:
        raise ValueError("The outline has no coordinates.")
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    return validate_bbox((min(lons), min(lats), max(lons), max(lats)))


def _boundary_from_polygons(name: str, polygons) -> Boundary:
    if not polygons:
        raise ValueError(
            f"{name} has no polygon. Points and lines do not outline an area."
        )
    footprint = {"type": "MultiPolygon", "coordinates": polygons}
    return Boundary(name=name, bbox=footprint_bounds(footprint), footprint=footprint)


def _round_footprint(footprint: dict):
    kind = footprint.get("type")
    coords = footprint.get("coordinates") or []

    def _pair(pair):
        return [round(float(pair[0]), 6), round(float(pair[1]), 6)]

    if kind == "Polygon":
        return {"type": kind, "coordinates": [[_pair(p) for p in ring] for ring in coords]}
    if kind == "MultiPolygon":
        return {
            "type": kind,
            "coordinates": [
                [[_pair(p) for p in ring] for ring in polygon]
                for polygon in coords
            ],
        }
    raise ValueError(f"Outline type {kind!r} is not a polygon.")


def _iter_lonlat(footprint: dict):
    kind = footprint.get("type")
    coords = footprint.get("coordinates") or []
    polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    if kind not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"Outline type {kind!r} is not a polygon.")
    for polygon in polygons:
        for ring in polygon:
            for pair in ring:
                if not pair or len(pair) < 2:
                    continue
                yield float(pair[0]), float(pair[1])


def _polygons_from_geojson(payload) -> list:
    if not isinstance(payload, dict):
        raise ValueError("GeoJSON should be an object.")
    kind = payload.get("type")
    if kind == "FeatureCollection":
        found = []
        for feature in payload.get("features") or []:
            found.extend(_polygons_from_geojson(feature))
        return found
    if kind == "Feature":
        return _polygons_from_geojson(payload.get("geometry") or {})
    if kind == "GeometryCollection":
        found = []
        for geometry in payload.get("geometries") or []:
            found.extend(_polygons_from_geojson(geometry))
        return found
    if kind == "Polygon":
        rings = _closed_rings(payload.get("coordinates") or [])
        return [rings] if rings else []
    if kind == "MultiPolygon":
        found = []
        for polygon in payload.get("coordinates") or []:
            rings = _closed_rings(polygon)
            if rings:
                found.append(rings)
        return found
    return []


def _closed_rings(polygon) -> list:
    rings = []
    for ring in polygon or []:
        points = []
        for pair in ring:
            if not pair or len(pair) < 2:
                continue
            points.append([float(pair[0]), float(pair[1])])
        if len(points) >= 3:
            if points[0] != points[-1]:
                points.append(points[0])
            rings.append(points)
    return rings


def _polygons_from_kml(text: str) -> list:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("That KML could not be parsed.") from exc
    polygons = []
    for element in root.iter():
        if _xml_name(element.tag) != "Polygon":
            continue
        rings = []
        for ring in element.iter():
            if _xml_name(ring.tag) != "LinearRing":
                continue
            for child in ring:
                if _xml_name(child.tag) != "coordinates" or not child.text:
                    continue
                points = _kml_coordinates(child.text)
                if len(points) >= 3:
                    if points[0] != points[-1]:
                        points.append(points[0])
                    rings.append(points)
        if rings:
            polygons.append(rings)
    return polygons


def _kml_coordinates(text: str) -> list:
    points = []
    for token in text.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        points.append([float(parts[0]), float(parts[1])])
    return points


def _xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _polygons_from_shp(data: bytes) -> list:
    """Polygon records out of a .shp. No .shx and no .dbf required.

    Shape type 5 is a polygon. 15 and 25 are the same XY with Z
    or M stuck on after the points, which I don't read.
    """
    if len(data) < 100:
        raise ValueError("File is too small to be a shapefile.")
    file_code = struct.unpack_from(">i", data, 0)[0]
    if file_code != 9994:
        raise ValueError("Not a shapefile (bad file code).")
    shape_type = struct.unpack_from("<i", data, 32)[0]
    if shape_type not in (5, 15, 25):
        raise ValueError(
            f"Shapefile shape type {shape_type} is not a polygon. "
            "Points and lines do not outline an area."
        )
    polygons = []
    pos = 100
    while pos + 8 <= len(data):
        _number, words = struct.unpack_from(">2i", data, pos)
        pos += 8
        record = data[pos:pos + words * 2]
        pos += words * 2
        if len(record) < 44:
            continue
        if struct.unpack_from("<i", record, 0)[0] == 0:
            continue
        n_parts, n_points = struct.unpack_from("<2i", record, 36)
        if n_parts < 1 or n_points < 3:
            continue
        part_at = 44
        parts = struct.unpack_from(f"<{n_parts}i", record, part_at)
        points_at = part_at + 4 * n_parts
        rings = []
        for index, start in enumerate(parts):
            stop = parts[index + 1] if index + 1 < n_parts else n_points
            ring = []
            for point in range(start, stop):
                lon, lat = struct.unpack_from("<2d", record, points_at + point * 16)
                ring.append([lon, lat])
            if len(ring) >= 3:
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                rings.append(ring)
        if rings:
            polygons.append(rings)
    return polygons


def _polygons_from_shapefile_zip(data: bytes, fallback_name: str):
    tmp = Path(tempfile.mkdtemp(prefix="dem_shp_"))
    try:
        dest = tmp / "out"
        dest.mkdir()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _safe_extract(archive, dest)
        shapes = [
            path for path in dest.rglob("*.shp")
            if "__MACOSX" not in path.parts and not path.name.startswith("._")
        ]
        if not shapes:
            raise ValueError(
                "No .shp file in that zip. Include the shapefile "
                "and its sidecars (.shx, .dbf, .prj)."
            )
        shapes.sort(key=lambda path: path.name.lower())
        shp = shapes[0]
        _reject_projected_prj(shp)
        polygons = _polygons_from_shp(shp.read_bytes())
        name = shp.name
        if len(shapes) > 1:
            name = f"{shp.name} (zip had {len(shapes)} .shp files; used the first)"
        return polygons, name or fallback_name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _reject_projected_prj(shp: Path) -> None:
    prj = shp.with_suffix(".prj")
    if not prj.is_file():
        return
    wkt = prj.read_text(errors="ignore").strip().upper()
    if wkt.startswith("PROJCS"):
        raise ValueError(
            f"{shp.name} is in a projected coordinate system. "
            "Reproject it to WGS84 (EPSG:4326) first. "
            "Automatic reprojection is not built."
        )


def _zip_has_shp(data: bytes) -> bool:
    return _zip_has_suffix(data, ".shp")


def _zip_has_kml(data: bytes) -> bool:
    return _zip_has_suffix(data, ".kml")


def _zip_has_suffix(data: bytes, suffix: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return any(name.lower().endswith(suffix) for name in archive.namelist())
    except zipfile.BadZipFile:
        return False


def _kml_text_from_zip(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("That KMZ is not a zip file.") from exc
    names = [
        name for name in archive.namelist()
        if name.lower().endswith(".kml") and not name.startswith("__MACOSX")
    ]
    if not names:
        raise ValueError("No .kml file inside that KMZ.")
    names.sort()
    return archive.read(names[0]).decode("utf-8", errors="replace")


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for info in archive.infolist():
        name = info.filename
        if not name or name.endswith("/"):
            continue
        target = (dest / name).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"Unsafe path in zip: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, open(target, "wb") as out:
            out.write(src.read())
