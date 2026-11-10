"""USGS 3DEP bare-earth DEM via the public ImageServer.

The server clips and reprojects, so this file only needs requests.
A grid longer than 8000 pixels on a side is several of those
requests, sliced from the same snapped grid and stitched back.

Asking for 1 m pixels does not mean the source is 1 m lidar.
Where the catalog's finest cell is coarser, the server
resamples and stays quiet. I record both numbers on the result.
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import requests

from .base import DemProvider
from ..composite import (
    NODATA,
    composite_arrays,
    footprint_mask,
    read_float_tiff,
    write_float_tiff,
    write_grid_tiff,
)
from ..tiles import crop_to_core, request_bounds, tile_windows
from ..aoi import (
    bbox_to_utm,
    epsg_nad83_utm,
    estimate_mb,
    snap_outward,
    validate_bbox,
)
from ..collections import build_catalog
from ..models import BBox, DemCatalog, DemEstimate, DemRequest, DemResult


# Read from the service's own ?f=json on Nov 6, 2026.
# 8000 x 8000 float32 is 256 MB. Tiling comes later.
MAX_PIXELS = 8000

BASE_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/"
    "3DEPElevation/ImageServer"
)

# Names go into a where clause. Anything else could change the
# query, so I refuse it instead of sending it.
_NAME_OK = re.compile(r"^[A-Za-z0-9_.]+$")


@dataclass(frozen=True)
class Coverage:
    """What the catalog says overlaps a box. Finest first."""

    resolutions_m: Tuple[float, ...]
    finest_name: str
    vertical_datum: str

    @property
    def finest_m(self) -> float:
        return self.resolutions_m[0]

    @property
    def coarsest_m(self) -> float:
        return self.resolutions_m[-1]


@dataclass(frozen=True)
class PixelGrid:
    """The UTM box actually sent to exportImage."""

    epsg: int
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    width_px: int
    height_px: int
    resolution_m: float


def coverage_from_payload(payload: dict) -> Coverage:
    """Turn an ImageServer query response into source cell sizes.

    LowPS is the native cell size in meters (1, ~3.4, ~10.3, ~30.9
    in the Nov 6 checks). Resolution_X is not: degrees on some
    rows, empty on the new lidar. I round LowPS to 2 decimals so
    10.307 and 10.308 are one product.

    Category=1 belongs in the query, not here. Overview rows
    (Category=2) go out to tens of kilometers and are not sources.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"3DEP catalog returned {type(payload).__name__}, expected JSON."
        )
    error = payload.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        raise ValueError(f"3DEP catalog error: {message}")

    rows = []
    for feature in payload.get("features") or []:
        attrs = feature.get("attributes") or {}
        low = attrs.get("LowPS")
        if low is None:
            continue
        try:
            low_m = float(low)
        except (TypeError, ValueError):
            continue
        rows.append((low_m, attrs))

    if not rows:
        raise ValueError(
            "3DEP has no elevation datasets for this box."
        )

    def _rank(item):
        low_m, attrs = item
        best = attrs.get("Best")
        # Lower Best won at Johnson Creek (the newest lidar).
        # Missing Best sorts last.
        try:
            best_n = float(best)
        except (TypeError, ValueError):
            best_n = 1e12
        return (low_m, best_n)

    rows.sort(key=_rank)
    _, winner = rows[0]
    resolutions = tuple(sorted({round(low_m, 2) for low_m, _ in rows}))
    datum = winner.get("VerticalDatum") or "not reported"
    return Coverage(
        resolutions_m=resolutions,
        finest_name=str(winner.get("Name") or "unnamed dataset"),
        vertical_datum=str(datum),
    )


def pixel_grid(
    bbox,
    resolution_m: float,
) -> PixelGrid:
    """Project and snap outward. A long side is fine.

    The 8000 pixel server limit is applied later, by splitting
    this grid. Refusing here would make a river reach at 1 m
    impossible to even estimate.
    """
    if resolution_m <= 0:
        raise ValueError(f"resolution_m must be > 0, got {resolution_m}")
    zone, xmin, ymin, xmax, ymax = bbox_to_utm(bbox)
    xmin, ymin, xmax, ymax, width, height = snap_outward(
        xmin, ymin, xmax, ymax, resolution_m
    )
    return PixelGrid(
        epsg=epsg_nad83_utm(zone),
        xmin=xmin,
        ymin=ymin,
        xmax=xmax,
        ymax=ymax,
        width_px=width,
        height_px=height,
        resolution_m=resolution_m,
    )


def mosaic_rule_for(collection) -> dict:
    """exportImage mosaicRule that keeps this collection only.

    The same n##w### name exists at about 10 m and about 30 m,
    so the name alone is not enough. LowPS within 0.2 m of the
    collection's own cell size separates them. The band is on
    the native cell, not the pixel size we asked to resample to.
    """
    names = tuple(collection.tile_names)
    bad = [name for name in names if not _NAME_OK.match(name)]
    if bad or not names:
        shown = ", ".join(bad) if bad else "none"
        raise ValueError(
            f"{collection.name} has no dataset name I can lock ({shown})."
        )
    quoted = ", ".join(f"'{name}'" for name in names)
    low = collection.resolution_m - 0.2
    high = collection.resolution_m + 0.2
    where = (
        f"Name IN ({quoted}) AND LowPS >= {low:.2f} AND LowPS <= {high:.2f}"
    )
    return {
        "mosaicMethod": "esriMosaicAttribute",
        "where": where,
        "mosaicOperation": "MT_FIRST",
    }


def describe_coverage(coverage: Coverage) -> str:
    return (
        f"USGS 3DEP ImageServer. Finest source {coverage.finest_name} "
        f"at {coverage.finest_m:g} m. Coarsest source cell in the box "
        f"is {coverage.coarsest_m:g} m. Vertical datum: "
        f"{coverage.vertical_datum}."
    )


class ThreeDepProvider(DemProvider):
    """Concrete DemProvider. Constructing this does not touch the network."""

    def __init__(
        self,
        timeout: int = 90,
        verify_ssl: bool = True,
        max_pixels: int = MAX_PIXELS,
    ):
        if max_pixels < 1:
            raise ValueError(f"max_pixels must be >= 1, got {max_pixels}")
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        # One exportImage can be this many pixels on a side.
        # Smaller in tests, so a short box still has to tile.
        self.max_pixels = max_pixels
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "TributaryToolkit ThreeDepProvider",
            "Accept": "image/tiff, application/json",
        })
        # estimate() then fetch() would otherwise ask the catalog
        # twice. The catalog answers with 502 often enough that
        # the extra calls hurt. Keyed by the rounded lon/lat box.
        self._coverage_cache = {}
        self._collection_cache = {}
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(
                urllib3.exceptions.InsecureRequestWarning
            )

    @property
    def key(self) -> str:
        return "3dep"

    @property
    def display_name(self) -> str:
        return "USGS 3DEP"

    def available_resolutions(self, bbox: BBox) -> List[float]:
        return list(self._coverage(bbox).resolutions_m)

    def list_collections(self, bbox: BBox) -> DemCatalog:
        """Picker rows for this box. Does not download a raster.

        Footprints come back in lon/lat. Tiles that share a project
        name are one choice. The national 10 m and 30 m grids are
        their own choices.
        """
        west, south, east, north = validate_bbox(bbox)
        key = tuple(round(value, 6) for value in (west, south, east, north))
        cached = self._collection_cache.get(key)
        if cached is not None:
            return cached
        features = self._query_features(
            west, south, east, north,
            out_fields=(
                "Name,LowPS,Best,VerticalDatum,DEM_Type,"
                "AcquisitionDate,StartDate,EndDate"
            ),
            return_geometry=True,
            out_sr=4326,
        )
        catalog = build_catalog((west, south, east, north), features)
        self._collection_cache[key] = catalog
        return catalog

    def estimate(self, request: DemRequest) -> DemEstimate:
        coverage, grid = self._prepare(request)
        return DemEstimate(
            provider=self.key,
            resolution_m=grid.resolution_m,
            width_px=grid.width_px,
            height_px=grid.height_px,
            estimated_mb=estimate_mb(grid.width_px, grid.height_px),
            n_requests=self._tile_count(grid),
            over_cap=False,
            source_resolution_m=coverage.finest_m,
            coarsest_resolution_m=coverage.coarsest_m,
        )

    def fetch(self, request: DemRequest, out_path: str) -> DemResult:
        coverage, grid = self._prepare(request)
        windows = self._windows(grid)
        path = Path(out_path)
        description = describe_coverage(coverage)
        if len(windows) == 1:
            # The bytes the server sent, unchanged. A stitch would
            # rewrite the file and drop whatever else the server put
            # in the tags.
            content = self._export(self._request_grid(grid, windows[0]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        else:
            stitched = self._stitch_values(grid, windows, mosaic_rule=None)
            write_grid_tiff(
                stitched,
                path,
                epsg=grid.epsg,
                xmin=grid.xmin,
                ymax=grid.ymax,
                resolution_m=grid.resolution_m,
            )
            description += (
                f" Stitched from {len(windows)} requests of the same blend."
            )
        return DemResult(
            path=str(path),
            provider=self.key,
            requested_resolution_m=grid.resolution_m,
            source_resolution_m=coverage.finest_m,
            coarsest_resolution_m=coverage.coarsest_m,
            epsg=grid.epsg,
            bbox_projected=(grid.xmin, grid.ymin, grid.xmax, grid.ymax),
            vertical_unit="m",
            source_description=description,
            from_cache=False,
        )

    def locked_estimate(self, request, resolution_m, primary, fill=None):
        """Size the grid we will actually request, not the mosaic's finest."""
        if request.provider != self.key:
            raise ValueError(
                f"This provider is {self.key!r}, the request asked "
                f"for {request.provider!r}."
            )
        grid = pixel_grid(request.bbox, float(resolution_m))
        coarsest = float(primary.resolution_m)
        if fill is not None and fill.resolution_m > coarsest:
            coarsest = float(fill.resolution_m)
        per_tile = 2 if fill is not None else 1
        return DemEstimate(
            provider=self.key,
            resolution_m=grid.resolution_m,
            width_px=grid.width_px,
            height_px=grid.height_px,
            estimated_mb=estimate_mb(grid.width_px, grid.height_px),
            n_requests=self._tile_count(grid) * per_tile,
            over_cap=False,
            source_resolution_m=float(primary.resolution_m),
            coarsest_resolution_m=coarsest,
        )

    def fetch_locked(
        self,
        request: DemRequest,
        out_path: str,
        primary,
        fill,
        resolution_m: float,
    ) -> DemResult:
        """One collection, holes punched, optional fill under it.

        Both exports use the same snapped grid so the arrays line
        up cell for cell. The where-clause keeps the server from
        blending in another year. I still mask with the footprint,
        because a locked export writes 0 in the holes and 0 is a
        real elevation.
        """
        if request.provider != self.key:
            raise ValueError(
                f"This provider is {self.key!r}, the request asked "
                f"for {request.provider!r}."
            )
        grid = pixel_grid(request.bbox, float(resolution_m))
        windows = self._windows(grid)
        path = Path(out_path)
        if len(windows) == 1:
            stacked, template = self._lock_one(grid, windows[0], primary, fill)
            write_float_tiff(template, stacked, path, NODATA)
        else:
            # Same mosaic rule on every piece. The stitch is one
            # grid, so a later reader cannot tell the pieces apart.
            stacked = self._lock_many(grid, windows, primary, fill)
            write_grid_tiff(
                stacked,
                path,
                epsg=grid.epsg,
                xmin=grid.xmin,
                ymax=grid.ymax,
                resolution_m=grid.resolution_m,
                nodata=NODATA,
            )
        coarsest = float(primary.resolution_m)
        if fill is not None and fill.resolution_m > coarsest:
            coarsest = float(fill.resolution_m)
        return DemResult(
            path=str(path),
            provider=self.key,
            requested_resolution_m=grid.resolution_m,
            source_resolution_m=float(primary.resolution_m),
            coarsest_resolution_m=coarsest,
            epsg=grid.epsg,
            bbox_projected=(grid.xmin, grid.ymin, grid.xmax, grid.ymax),
            vertical_unit="m",
            source_description="",
            from_cache=False,
            collection_key=primary.key,
            collection_name=primary.name,
            collection_period=primary.period_label(),
            fill_key=None if fill is None else fill.key,
            fill_name=None if fill is None else fill.name,
            fill_period=None if fill is None else fill.period_label(),
            fill_used=fill is not None,
            nodata=NODATA,
        )

    def _tile_count(self, grid: PixelGrid) -> int:
        return len(self._windows(grid))

    def _windows(self, grid: PixelGrid):
        return tile_windows(
            grid.width_px, grid.height_px, self.max_pixels
        )

    def _request_grid(self, grid: PixelGrid, window) -> PixelGrid:
        xmin, ymin, xmax, ymax, width, height = request_bounds(
            grid.xmin, grid.ymax, grid.resolution_m, window
        )
        return PixelGrid(
            epsg=grid.epsg,
            xmin=xmin,
            ymin=ymin,
            xmax=xmax,
            ymax=ymax,
            width_px=width,
            height_px=height,
            resolution_m=grid.resolution_m,
        )

    def _download_core(self, grid: PixelGrid, window, mosaic_rule, label):
        """One request, halo cropped off. Core only."""
        requested = self._request_grid(grid, window)
        values, image = read_float_tiff(
            self._export(requested, mosaic_rule)
        )
        self._check_shape(values, requested, label)
        return crop_to_core(values, window), image

    def _stitch_values(self, grid: PixelGrid, windows, mosaic_rule):
        out = np.empty(
            (grid.height_px, grid.width_px), dtype=np.float32
        )
        for window in windows:
            core, _image = self._download_core(
                grid, window, mosaic_rule, "3DEP"
            )
            out[
                window.row0:window.row1,
                window.col0:window.col1,
            ] = core
        return out

    def _lock_one(self, grid: PixelGrid, window, primary, fill):
        """One locked request. Returns the stacked core and the server image.

        The server image is only used for its georef tags, and only
        when this is the whole grid (no stitch).
        """
        primary_core, image = self._download_core(
            grid, window, mosaic_rule_for(primary), primary.name
        )
        fill_core = None
        if fill is not None:
            fill_core, _fill_image = self._download_core(
                grid, window, mosaic_rule_for(fill), fill.name
            )
        stacked = self._stack_core(
            grid, window, primary, primary_core, fill, fill_core
        )
        return stacked, image

    def _lock_many(self, grid: PixelGrid, windows, primary, fill):
        out = np.full(
            (grid.height_px, grid.width_px), NODATA, dtype=np.float32
        )
        for window in windows:
            primary_core, _image = self._download_core(
                grid, window, mosaic_rule_for(primary), primary.name
            )
            fill_core = None
            if fill is not None:
                fill_core, _fill_image = self._download_core(
                    grid, window, mosaic_rule_for(fill), fill.name
                )
            out[
                window.row0:window.row1,
                window.col0:window.col1,
            ] = self._stack_core(
                grid, window, primary, primary_core, fill, fill_core
            )
        return out

    def _stack_core(self, grid, window, primary, primary_core, fill, fill_core):
        zone = grid.epsg - 26900
        xmin = grid.xmin + window.col0 * grid.resolution_m
        ymax = grid.ymax - window.row0 * grid.resolution_m
        height, width = primary_core.shape
        primary_inside = footprint_mask(
            primary.footprint,
            zone,
            xmin,
            ymax,
            width,
            height,
            grid.resolution_m,
        )
        fill_inside = None
        if fill is not None:
            fill_inside = footprint_mask(
                fill.footprint,
                zone,
                xmin,
                ymax,
                width,
                height,
                grid.resolution_m,
            )
        return composite_arrays(
            primary_core, primary_inside, fill_core, fill_inside
        )

    def _check_shape(self, values, grid: PixelGrid, name: str) -> None:
        height, width = values.shape
        if width != grid.width_px or height != grid.height_px:
            raise ValueError(
                f"{name} came back {width} x {height} px, "
                f"I asked for {grid.width_px} x {grid.height_px}."
            )

    def _prepare(self, request: DemRequest) -> Tuple[Coverage, PixelGrid]:
        if request.provider != self.key:
            raise ValueError(
                f"This provider is {self.key!r}, the request asked "
                f"for {request.provider!r}."
            )
        coverage = self._coverage(request.bbox)
        if request.resolution_m == "best":
            resolution = coverage.finest_m
        else:
            resolution = float(request.resolution_m)
        return coverage, pixel_grid(request.bbox, resolution)

    def _coverage(self, bbox: BBox) -> Coverage:
        west, south, east, north = validate_bbox(bbox)
        key = tuple(round(value, 6) for value in (west, south, east, north))
        cached = self._coverage_cache.get(key)
        if cached is not None:
            return cached
        coverage = self._query_coverage(west, south, east, north)
        self._coverage_cache[key] = coverage
        return coverage

    def _query_coverage(self, west, south, east, north) -> Coverage:
        features = self._query_features(
            west, south, east, north,
            out_fields="Name,LowPS,Best,VerticalDatum",
            return_geometry=False,
        )
        return coverage_from_payload({"features": features})

    def _query_features(
        self,
        west,
        south,
        east,
        north,
        out_fields: str,
        return_geometry: bool,
        out_sr: Optional[int] = None,
    ) -> list:
        # JSON, not str(dict). requests would otherwise send
        # single quotes and the server rejects the geometry.
        geometry = json.dumps({
            "xmin": west,
            "ymin": south,
            "xmax": east,
            "ymax": north,
            "spatialReference": {"wkid": 4326},
        })
        features = []
        offset = 0
        # Geometry responses are much larger than attribute rows.
        page = 200 if return_geometry else 1000
        while True:
            params = {
                "geometry": geometry,
                "geometryType": "esriGeometryEnvelope",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "where": "Category=1",
                "outFields": out_fields,
                "returnGeometry": "true" if return_geometry else "false",
                "orderByFields": "OBJECTID",
                "resultOffset": offset,
                "resultRecordCount": page,
                "f": "json",
            }
            if out_sr is not None:
                params["outSR"] = out_sr
            payload = self._get_json(BASE_URL + "/query", params)
            if payload.get("error"):
                # Raises ValueError with the server message.
                coverage_from_payload(payload)
            batch = payload.get("features") or []
            features.extend(batch)
            hit_limit = bool(payload.get("exceededTransferLimit"))
            if not batch or (not hit_limit and len(batch) < page):
                break
            offset += len(batch)
            if offset > 20000:
                break
        return features

    def _export(self, grid: PixelGrid, mosaic_rule: Optional[dict] = None) -> bytes:
        params = {
            "bbox": f"{grid.xmin},{grid.ymin},{grid.xmax},{grid.ymax}",
            "bboxSR": grid.epsg,
            "imageSR": grid.epsg,
            "size": f"{grid.width_px},{grid.height_px}",
            "format": "tiff",
            "pixelType": "F32",
            # Explicit so a resampled coarse DEM is repeatable.
            # At native resolution this is nearly a no-op.
            "interpolation": "RSP_BilinearInterpolation",
            "f": "image",
        }
        if mosaic_rule is not None:
            params["mosaicRule"] = json.dumps(mosaic_rule)
            # Uncompressed locked TIFFs came back as a header only
            # (tile offset 0, 1160 bytes) on Nov 10. LZW files
            # opened and had real elevations. The unlocked mosaic
            # still asks for no compression, which does work.
            params["compression"] = "LZW"
        return self._get_bytes(BASE_URL + "/exportImage", params)

    def _get_json(self, url: str, params: dict) -> dict:
        body = self._send(url, params, expect_tiff=False)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ValueError(
                f"3DEP did not return JSON from {url}."
            ) from exc

    def _get_bytes(self, url: str, params: dict) -> bytes:
        return self._send(url, params, expect_tiff=True)

    def _send(self, url: str, params: dict, expect_tiff: bool) -> bytes:
        """GET with retries. 5xx and a non-TIFF body are worth another try.

        A 4xx is the request's fault. Retrying it won't help.
        The Nov 6 spike saw a 502 on a download that succeeded
        on the next attempt.
        """
        last_error = "no response"
        # 502/504 showed up several times on Nov 6. Four tries was
        # not always enough during the Johnson Creek smoke.
        for attempt in range(6):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            except requests.exceptions.SSLError:
                # A work-network interceptor presents its own
                # certificate. Retrying with the same verify flag
                # will fail the same way. The caller turns
                # verify_ssl off; I don't decide that here.
                raise
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(min(8, 2 ** attempt))
                continue
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                time.sleep(min(8, 2 ** attempt))
                continue
            if response.status_code >= 400:
                raise requests.HTTPError(
                    f"3DEP request failed: HTTP {response.status_code} "
                    f"from {url}"
                )
            raw = response.content
            if expect_tiff:
                if raw[:4] == b"II*\x00":
                    return raw
                last_error = f"not a TIFF ({raw[:40]!r})"
                time.sleep(min(8, 2 ** attempt))
                continue
            return raw
        name = url.rstrip("/").rsplit("/", 1)[-1]
        raise requests.RequestException(
            f"3DEP {name} failed after retries: {last_error}"
        )
