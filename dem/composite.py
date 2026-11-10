"""Stack one locked DEM on top of an optional fill.

The ImageServer will lock exportImage to one dataset, but on
Nov 10 a locked TIFF had 0 outside that dataset and no nodata
tag. 0 m is a real elevation, so I cannot trust it. I keep a
pixel only when its center falls inside the footprint I already
stored, and I write -999999 everywhere else.

Primary wins. The fill is written first and only outside the
primary footprint, then the primary overwrites it.
"""

import io
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from PIL.TiffImagePlugin import ImageFileDirectory_v2

from .aoi import lonlat_to_utm

# USGS uses this as a missing elevation. Real ground in the US
# does not. Float32 can hold it exactly (it is under 2^23).
NODATA = -999999.0

# GeoTIFF tags I have to carry onto the file I rewrite.
# Pillow builds a new TIFF, and copying tile offsets from the
# server file would point at bytes that are no longer there.
_GEO_TAGS = (33550, 33922, 34735, 34736, 34737)


def describe_lock(primary, fill, fill_used: bool) -> str:
    """One sentence block for DemResult.source_description."""
    text = (
        f"Locked to {primary.name} ({primary.period_label()}), "
        f"native cell {primary.resolution_m:g} m. "
        f"Outside that footprint the pixel is nodata ({NODATA:.0f})."
    )
    if fill is None:
        return text + " No fill. Holes stay nodata."
    if not fill_used:
        return text + (
            f" {fill.name} ({fill.period_label()}) was not downloaded. "
            "The primary footprint already covers this box."
        )
    return text + (
        f" Holes only use {fill.name} ({fill.period_label()}), "
        f"native cell {fill.resolution_m:g} m, resampled onto this grid. "
        "Where the primary footprint has data, that value stays."
    )


def read_float_tiff(raw: bytes):
    """Float32 array plus the Pillow image (for its georef tags).

    Shape is (height, width), row 0 at the north edge, which is
    how the 3DEP GeoTIFF is stored.
    """
    if raw[:4] != b"II*\x00":
        raise ValueError(f"3DEP did not return a TIFF ({raw[:40]!r}).")
    image = Image.open(io.BytesIO(raw))
    image.load()
    if image.mode != "F":
        raise ValueError(f"Expected a float TIFF, got mode {image.mode}.")
    values = np.array(image, dtype=np.float32)
    return values, image


def write_grid_tiff(
    values,
    out_path,
    epsg: int,
    xmin: float,
    ymax: float,
    resolution_m: float,
    nodata: Optional[float] = None,
) -> None:
    """Write a float grid with georeferencing we build ourselves.

    Used when the file is a stitch of several server tiles. Those
    tiles each carry their own tie point, which would be wrong for
    the combined grid. Row 0 is north, so the upper-left corner is
    (xmin, ymax). Pixel-is-area means that corner is the outer
    corner of pixel (0, 0), matching the snapped grid.
    """
    out = Image.fromarray(
        np.ascontiguousarray(values, dtype=np.float32), mode="F"
    )
    info = ImageFileDirectory_v2()
    info[33550] = (float(resolution_m), float(resolution_m), 0.0)
    info[33922] = (0.0, 0.0, 0.0, float(xmin), float(ymax), 0.0)
    info[34735] = (
        1, 1, 0, 3,
        1024, 0, 1, 1,
        1025, 0, 1, 1,
        3072, 0, 1, int(epsg),
    )
    if nodata is not None:
        info[42113] = f"{nodata:.0f}"
        info.tagtype[42113] = 2  # ASCII
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path, format="TIFF", tiffinfo=info)


def write_float_tiff(template, values, out_path, nodata: float = NODATA) -> None:
    """Write values using the template image's georeferencing.

    values must be the same shape as the template. I set the
    GDAL nodata tag (42113) because the server TIFF does not
    have one.
    """
    height, width = values.shape
    if template.size != (width, height):
        raise ValueError(
            f"Raster is {width} x {height} px, template is {template.size}."
        )
    out = Image.fromarray(
        np.ascontiguousarray(values, dtype=np.float32), mode="F"
    )
    info = ImageFileDirectory_v2()
    source = template.tag_v2
    for tag in _GEO_TAGS:
        if tag not in source:
            continue
        info[tag] = source[tag]
        if tag in source.tagtype:
            info.tagtype[tag] = source.tagtype[tag]
    info[42113] = f"{nodata:.0f}"
    info.tagtype[42113] = 2  # ASCII
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path, format="TIFF", tiffinfo=info)


def punch_outside_file(
    path,
    footprint,
    epsg: int,
    xmin: float,
    ymax: float,
    resolution_m: float,
) -> None:
    """Set nodata on pixels whose center is outside the outline.

    The file is already a float GeoTIFF of the rectangle. I rewrite
    it in place. A pixel inside a collection hole stays nodata.
    A pixel outside the uploaded polygon becomes nodata too.
    """
    if not 26901 <= int(epsg) <= 26960:
        raise ValueError(f"Expected a NAD83 UTM EPSG, got {epsg}.")
    raw = Path(path).read_bytes()
    values, image = read_float_tiff(raw)
    height, width = values.shape
    inside = footprint_mask(
        footprint,
        int(epsg) - 26900,
        xmin,
        ymax,
        width,
        height,
        resolution_m,
    )
    if not bool(inside.any()):
        raise ValueError(
            "The outline does not cover any pixel of this grid."
        )
    masked = np.array(values, dtype=np.float32, copy=True)
    masked[~inside] = NODATA
    write_float_tiff(image, masked, path, NODATA)


def footprint_mask(footprint, zone, xmin, ymax, width, height, resolution_m):
    """True where the pixel center is inside the lon/lat footprint.

    xmin / ymax are the outer edges of the snapped UTM grid.
    Row 0 is north. A missing footprint is all False: I would
    rather punch the whole raster out than invent coverage.
    """
    mask = np.zeros((height, width), dtype=bool)
    if not footprint or width <= 0 or height <= 0:
        return mask
    xs = xmin + (np.arange(width) + 0.5) * resolution_m
    ys = ymax - (np.arange(height) + 0.5) * resolution_m
    for rings in _polygons(footprint):
        projected = [
            [lonlat_to_utm(lon, lat, zone)[1:] for lon, lat in ring]
            for ring in rings
        ]
        mask |= _polygon_hits(projected, xs, ys)
    return mask


def composite_arrays(primary, primary_inside, fill=None, fill_inside=None):
    """Primary overwrites fill. Fill never lands inside the primary.

    A non-finite primary pixel stays nodata even if the fill has
    a value there. That cell is still the primary's ground.
    """
    primary = np.asarray(primary, dtype=np.float32)
    out = np.full(primary.shape, NODATA, dtype=np.float32)
    inside = np.asarray(primary_inside, dtype=bool)
    if inside.shape != primary.shape:
        raise ValueError("Primary mask shape does not match the raster.")
    if fill is not None:
        fill = np.asarray(fill, dtype=np.float32)
        holes = np.asarray(fill_inside, dtype=bool)
        if fill.shape != primary.shape or holes.shape != primary.shape:
            raise ValueError("Fill raster does not match the primary grid.")
        use_fill = holes & ~inside & np.isfinite(fill)
        out[use_fill] = fill[use_fill]
    use_primary = inside & np.isfinite(primary)
    out[use_primary] = primary[use_primary]
    return out


def _polygons(footprint):
    kind = footprint.get("type")
    coords = footprint.get("coordinates") or []
    if kind == "Polygon":
        polygons = [coords]
    elif kind == "MultiPolygon":
        polygons = coords
    else:
        return []
    parsed = []
    for polygon in polygons:
        rings = []
        for ring in polygon:
            points = [
                (float(pair[0]), float(pair[1]))
                for pair in ring
                if pair and len(pair) >= 2
            ]
            if len(points) >= 3:
                rings.append(points)
        if rings:
            parsed.append(rings)
    return parsed


def _polygon_hits(rings, xs, ys):
    """Even-odd across the rings of one polygon. Holes un-fill."""
    inside = np.zeros((ys.size, xs.size), dtype=bool)
    for ring in rings:
        inside ^= _ring_hits(ring, xs, ys)
    return inside


def _ring_hits(ring, xs, ys):
    """Same even-odd test as collections._point_in_ring, for a grid."""
    pts = np.asarray(ring, dtype=np.float64)
    count = pts.shape[0]
    inside = np.zeros((ys.size, xs.size), dtype=bool)
    if count < 3:
        return inside
    previous = count - 1
    for index in range(count):
        xi, yi = pts[index]
        xj, yj = pts[previous]
        previous = index
        crosses = (yi > ys) != (yj > ys)
        if not np.any(crosses):
            continue
        x_hit = (xj - xi) * (ys - yi) / (yj - yi) + xi
        rows = np.flatnonzero(crosses)
        inside[rows] ^= xs < x_hit[rows, None]
    return inside
