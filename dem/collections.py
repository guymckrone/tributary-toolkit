"""Group 3DEP catalog rows into picker choices. No network.

A project is published as one or many tiles that share a name.
The national 10 m and 30 m grids are tiles named like n46w123,
two rows each (one per resolution). The picker wants one row
per collection, a footprint, and how much of the user's box
that footprint covers.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from .aoi import bbox_to_utm, lonlat_to_utm, validate_bbox
from .models import DemCatalog, DemCollection

# n46w123, n45w122. The 1-degree national tiles.
_NATIONAL_NAME = re.compile(r"^n\d{2}w\d{3}$")

# A sample on the boundary can fall outside by a fraction of a
# meter. 99.5% still means the dataset covers the box.
_COVERS_ALL_PCT = 99.5


def _parse_date(value):
    """Catalog dates arrive as 20240809, 200911, or 2009."""
    if value is None or value == "":
        return None
    text = str(value).split(".")[0]
    if not text.isdigit():
        return None
    if len(text) >= 8:
        return int(text[:4]), int(text[4:6]), int(text[6:8])
    if len(text) == 6:
        return int(text[:4]), int(text[4:6]), None
    if len(text) == 4:
        return int(text), None, None
    return None


def _date_text(value) -> Optional[str]:
    parsed = _parse_date(value)
    if not parsed:
        return None
    year, month, day = parsed
    if month and day:
        return f"{year:04d}-{month:02d}-{day:02d}"
    if month:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}"


def _date_rank(value):
    parsed = _parse_date(value)
    if not parsed:
        return None
    year, month, day = parsed
    return (year, month or 0, day or 0)


def _acquisition_year(value) -> Optional[int]:
    """AcquisitionDate is unix milliseconds."""
    if value is None or value == "":
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if stamp > 1e11:
        stamp = stamp / 1000.0
    if stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).year


def _resolution_label(resolution_m: float) -> str:
    if resolution_m < 2:
        return "1 m"
    if resolution_m < 5:
        return "3 m"
    if resolution_m < 15:
        return "10 m"
    if resolution_m < 45:
        return "30 m"
    return f"{resolution_m:.0f} m"


def _group_key(name: str, resolution_m: float) -> Tuple[str, str]:
    if _NATIONAL_NAME.match(name):
        return ("national", f"{resolution_m:.2f}")
    return ("project", f"{name}|{resolution_m:.2f}")


def _display_name(kind: str, name: str, resolution_m: float) -> str:
    if kind == "national":
        return f"3DEP national DEM ({_resolution_label(resolution_m)})"
    return name


def _rings_lonlat(geometry) -> List[List[Tuple[float, float]]]:
    rings = []
    for ring in (geometry or {}).get("rings") or []:
        points = []
        for pair in ring:
            if not pair or len(pair) < 2:
                continue
            lon, lat = float(pair[0]), float(pair[1])
            points.append((lon, lat))
        if len(points) >= 3:
            rings.append(points)
    return rings


def _close(ring):
    if ring and ring[0] != ring[-1]:
        return ring + [ring[0]]
    return ring


def _point_in_ring(x: float, y: float, ring) -> bool:
    """Even-odd ray cast. Boundary points are not guaranteed."""
    inside = False
    count = len(ring)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = ring[i]
        xj, yj = ring[j]
        crosses = (yi > y) != (yj > y)
        if crosses:
            # yi == yj cannot happen when one is above y and one is not.
            x_hit = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_hit:
                inside = not inside
        j = i
    return inside


def _point_in_rings(x: float, y: float, rings) -> bool:
    inside = False
    for ring in rings:
        if _point_in_ring(x, y, ring):
            inside = not inside
    return inside


def _project_rings(rings, zone: int):
    projected = []
    for ring in rings:
        projected.append([
            lonlat_to_utm(lon, lat, zone)[1:]
            for lon, lat in ring
        ])
    return projected


def _sample_grid(bbox, spacing_m: Optional[float] = None):
    """Cell centers inside the box, in the box's UTM zone.

    Returns (points, spacing_m). spacing grows so a big box stays
    under about 8000 samples.
    """
    zone, xmin, ymin, xmax, ymax = bbox_to_utm(bbox)
    width = xmax - xmin
    height = ymax - ymin
    longest = max(width, height, 1.0)
    if spacing_m is None:
        spacing_m = min(max(longest / 60.0, 20.0), 250.0)
    cols = max(1, int(width / spacing_m))
    rows = max(1, int(height / spacing_m))
    while cols * rows > 8000:
        spacing_m *= 1.3
        cols = max(1, int(width / spacing_m))
        rows = max(1, int(height / spacing_m))
    points = []
    for row in range(rows):
        y = ymin + (row + 0.5) * height / rows
        for col in range(cols):
            x = xmin + (col + 0.5) * width / cols
            points.append((x, y))
    return zone, points, spacing_m


def suggest_key(collections: Sequence[DemCollection]) -> Optional[str]:
    """Preselect the newest lidar that actually covers the box.

    A project has to cover at least half the box before it beats
    the national grid. A 10% sliver of new lidar is a poor default.
    Among full-coverage national grids, the finer one wins.
    """

    def _best(pool: Sequence[DemCollection]) -> DemCollection:
        return max(
            pool,
            key=lambda item: (
                item.coverage_pct,
                item.year or 0,
                -item.resolution_m,
            ),
        )

    lidar = [
        item for item in collections
        if item.kind == "project"
        and item.resolution_m <= 2
        and item.coverage_pct >= 50
    ]
    if lidar:
        return _best(lidar).key
    coarser = [
        item for item in collections
        if item.kind == "project"
        and item.resolution_m <= 5
        and item.coverage_pct >= 50
    ]
    if coarser:
        return _best(coarser).key
    national = [
        item for item in collections
        if item.kind == "national" and item.covers_all
    ]
    if national:
        return _best(national).key
    if collections:
        return _best(list(collections)).key
    return None


def build_catalog(bbox, features: Sequence[dict]) -> DemCatalog:
    """Turn ImageServer query features into picker rows.

    features are Esri JSON: attributes plus geometry.rings in lon/lat.
    """
    west, south, east, north = validate_bbox(bbox)
    bbox = (west, south, east, north)
    groups = {}
    for feature in features:
        attrs = feature.get("attributes") or {}
        name = str(attrs.get("Name") or "").strip()
        low = attrs.get("LowPS")
        if not name or low is None:
            continue
        try:
            resolution_m = round(float(low), 2)
        except (TypeError, ValueError):
            continue
        kind, key_tail = _group_key(name, resolution_m)
        key = f"{kind}:{key_tail}"
        bucket = groups.get(key)
        if bucket is None:
            bucket = {
                "key": key,
                "kind": kind,
                "name": name,
                "resolution_m": resolution_m,
                "tiles": [],
                "rings": [],
                "starts": [],
                "ends": [],
                "acq_years": [],
                "datums": [],
                "dem_types": [],
            }
            groups[key] = bucket
        bucket["tiles"].append(name)
        rings = _rings_lonlat(feature.get("geometry"))
        if rings:
            first = rings[0][0]
            if not (-180 <= first[0] <= 180 and -90 <= first[1] <= 90):
                raise ValueError(
                    "Catalog footprint is not lon/lat. "
                    "The query needs outSR=4326."
                )
            bucket["rings"].append(rings)
        start = attrs.get("StartDate")
        end = attrs.get("EndDate")
        if _date_rank(start):
            bucket["starts"].append(start)
        if _date_rank(end):
            bucket["ends"].append(end)
        acq = _acquisition_year(attrs.get("AcquisitionDate"))
        if acq:
            bucket["acq_years"].append(acq)
        datum = attrs.get("VerticalDatum")
        if datum:
            bucket["datums"].append(str(datum))
        dem_type = attrs.get("DEM_Type")
        if dem_type is not None and dem_type != "":
            try:
                bucket["dem_types"].append(int(dem_type))
            except (TypeError, ValueError):
                pass

    zone, samples, spacing_m = _sample_grid(bbox)
    collections = []
    for bucket in groups.values():
        projected = [
            _project_rings(rings, zone) for rings in bucket["rings"]
        ]
        if samples and projected:
            hits = 0
            for x, y in samples:
                if any(_point_in_rings(x, y, rings) for rings in projected):
                    hits += 1
            pct = 100.0 * hits / len(samples)
        else:
            pct = 0.0
        start_value = min(bucket["starts"], key=_date_rank) if bucket["starts"] else None
        end_value = max(bucket["ends"], key=_date_rank) if bucket["ends"] else None
        end_parsed = _parse_date(end_value)
        acq_year = max(bucket["acq_years"]) if bucket["acq_years"] else None
        year = end_parsed[0] if end_parsed else acq_year
        dem_types = set(bucket["dem_types"])
        polygons = []
        for rings in bucket["rings"]:
            polygons.append([
                [[lon, lat] for lon, lat in _close(ring)]
                for ring in rings
            ])
        footprint = (
            {"type": "MultiPolygon", "coordinates": polygons}
            if polygons else None
        )
        names = tuple(dict.fromkeys(bucket["tiles"]))
        collections.append(DemCollection(
            key=bucket["key"],
            name=_display_name(bucket["kind"], bucket["name"], bucket["resolution_m"]),
            kind=bucket["kind"],
            resolution_m=bucket["resolution_m"],
            year=year,
            start_text=_date_text(start_value),
            end_text=_date_text(end_value),
            acquisition_year=acq_year,
            coverage_pct=round(pct, 1),
            covers_all=pct >= _COVERS_ALL_PCT,
            vertical_datum=bucket["datums"][0] if bucket["datums"] else "",
            dem_type=dem_types.pop() if len(dem_types) == 1 else None,
            tile_count=len(bucket["tiles"]),
            tile_names=names,
            footprint=footprint,
        ))

    collections.sort(
        key=lambda item: (
            item.covers_all,
            item.coverage_pct,
            item.year or 0,
            -item.resolution_m,
        ),
        reverse=True,
    )
    frozen = tuple(collections)
    return DemCatalog(
        collections=frozen,
        suggested_key=suggest_key(frozen),
        coverage_spacing_m=round(spacing_m, 1),
    )
