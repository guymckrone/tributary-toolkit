"""Lon/lat boxes to a UTM pixel grid. No network, no files.

I project this myself (GRS80 transverse Mercator, which is what
NAD83 UTM uses) so the DEM backend doesn't need pyproj. Checked
against the 3DEP ImageServer on Nov 6, 2026: about 1.2 m off at
the Johnson Creek test point. That's one pixel at 1 m. Fine for
a download box. Revisit with pyproj if REM needs tighter.

Northern hemisphere only. EPSG is 269xx (NAD83 / UTM zone N).
"""

import math
from typing import Optional, Tuple

from .models import BBox


def validate_bbox(bbox) -> BBox:
    """(west, south, east, north) in lon/lat, or raise.

    Same shape as the site-search bbox. I reject projected
    coordinates (State Plane feet, etc.) the same way the
    shapefile reader does: values outside lon/lat range.
    """
    if bbox is None or len(bbox) != 4:
        raise ValueError(
            "bbox needs four numbers: west, south, east, north."
        )
    west, south, east, north = (float(value) for value in bbox)
    if not (
        -180 <= west <= 180 and -180 <= east <= 180
        and -90 <= south <= 90 and -90 <= north <= 90
    ):
        raise ValueError(
            "bbox is not in longitude/latitude "
            f"{(west, south, east, north)}. Projected coordinates "
            "(State Plane, UTM) need reprojecting to WGS84 first."
        )
    if west >= east or south >= north:
        raise ValueError(
            "bbox is empty or flipped (west < east, south < north)."
        )
    return (west, south, east, north)


def utm_zone(lon: float) -> int:
    """UTM zone 1-60. The meridian on a boundary goes to the eastern zone.

    lon -120 is the west edge of zone 11, so it returns 11.
    """
    if not -180 <= lon <= 180:
        raise ValueError(f"longitude out of range: {lon}")
    zone = int((lon + 180.0) / 6.0) + 1
    if zone > 60:
        zone = 60
    return zone


def epsg_nad83_utm(zone: int) -> int:
    """EPSG for NAD83 / UTM zone N. Northern hemisphere only."""
    if not 1 <= zone <= 60:
        raise ValueError(f"UTM zone out of range: {zone}")
    return 26900 + zone


def lonlat_to_utm(
    lon: float,
    lat: float,
    zone: Optional[int] = None,
) -> Tuple[int, float, float]:
    """Project one point. Returns (zone, easting_m, northing_m).

    Pass zone when the point should stay in a neighboring zone
    (a box that crosses a boundary is projected in one zone).
    """
    if not -180 <= lon <= 180:
        raise ValueError(f"longitude out of range: {lon}")
    if not 0 <= lat <= 84:
        raise ValueError(
            f"latitude {lat} is outside the northern UTM range "
            "this helper supports (0 to 84)."
        )
    if zone is None:
        zone = utm_zone(lon)
    elif not 1 <= zone <= 60:
        raise ValueError(f"UTM zone out of range: {zone}")

    # GRS80. NAD83 uses this ellipsoid.
    a = 6378137.0
    f = 1.0 / 298.257222101
    k0 = 0.9996
    e2 = f * (2.0 - f)
    ep2 = e2 / (1.0 - e2)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)

    sin_lat = math.sin(lat_r)
    cos_lat = math.cos(lat_r)
    n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    t = math.tan(lat_r) ** 2
    c = ep2 * cos_lat * cos_lat
    big_a = (lon_r - lon0) * cos_lat
    e4 = e2 * e2
    e6 = e4 * e2
    m = a * (
        (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat_r
        - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat_r)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat_r)
        - (35 * e6 / 3072) * math.sin(6 * lat_r)
    )
    easting = k0 * n * (
        big_a
        + (1 - t + c) * big_a ** 3 / 6
        + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * big_a ** 5 / 120
    ) + 500000.0
    northing = k0 * (
        m
        + n * math.tan(lat_r) * (
            big_a ** 2 / 2
            + (5 - t + 9 * c + 4 * c * c) * big_a ** 4 / 24
            + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * big_a ** 6 / 720
        )
    )
    return zone, easting, northing


def bbox_to_utm(
    bbox,
    zone: Optional[int] = None,
) -> Tuple[int, float, float, float, float]:
    """Project a lon/lat box into one UTM zone.

    Returns (zone, xmin, ymin, xmax, ymax). Zone defaults to the
    center longitude. A box that crosses a zone boundary stays in
    that one zone; the far edge is stretched a little, which is
    acceptable for a download extent.
    """
    west, south, east, north = validate_bbox(bbox)
    if zone is None:
        zone = utm_zone((west + east) / 2.0)
    corners = (
        (west, south),
        (west, north),
        (east, south),
        (east, north),
    )
    projected = [
        lonlat_to_utm(lon, lat, zone) for lon, lat in corners
    ]
    xs = [point[1] for point in projected]
    ys = [point[2] for point in projected]
    return zone, min(xs), min(ys), max(xs), max(ys)


def snap_outward(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    resolution_m: float,
) -> Tuple[float, float, float, float, int, int]:
    """Grow a meter box so its edges land on the pixel grid.

    The ImageServer only returns exact pixel sizes when
    size = extent / resolution and the extent is already in the
    output CRS. Snapping outward (never inward) keeps the
    caller's whole box inside the raster.

    Returns (xmin, ymin, xmax, ymax, width_px, height_px).
    """
    if resolution_m <= 0:
        raise ValueError(f"resolution_m must be > 0, got {resolution_m}")
    if xmin >= xmax or ymin >= ymax:
        raise ValueError("projected box is empty or flipped.")

    snapped_xmin = math.floor(xmin / resolution_m) * resolution_m
    snapped_ymin = math.floor(ymin / resolution_m) * resolution_m
    snapped_xmax = math.ceil(xmax / resolution_m) * resolution_m
    snapped_ymax = math.ceil(ymax / resolution_m) * resolution_m
    # round() guards a binary float like 10.0000000002 from
    # becoming 11 pixels.
    width_px = int(round((snapped_xmax - snapped_xmin) / resolution_m))
    height_px = int(round((snapped_ymax - snapped_ymin) / resolution_m))
    return (
        snapped_xmin,
        snapped_ymin,
        snapped_xmax,
        snapped_ymax,
        width_px,
        height_px,
    )


def estimate_mb(width_px: int, height_px: int) -> float:
    """Uncompressed Float32 size in decimal MB (1 MB = 1e6 bytes).

    Matches the size table in the DEM plan. Not the GeoTIFF's
    size on disk.
    """
    if width_px < 1 or height_px < 1:
        raise ValueError(
            f"pixel counts must be >= 1, got {width_px} x {height_px}"
        )
    return (width_px * height_px * 4) / 1_000_000
