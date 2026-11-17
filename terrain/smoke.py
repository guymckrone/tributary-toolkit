"""Live terrain prep check on Johnson Creek. 1 m lidar, about 600 m.

Run from data-downloader, in the geo env:

    conda run -n datadownload-geo python -m terrain.smoke

Uses the normal DEM cache, so after Slice 0 the DEM is usually a
hit and USGS is only asked for the collection list. The prep cache
for this run is under the system temp folder and is cleared first,
so the first prep is a real Whitebox run.
"""

import math
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from dem.registry import create_provider
from dem.service import list_collections

from .models import PrepParams, TerrainAoi
from .service import prepare_terrain

# Same box as the Slice 0 spike, so the DEM cache file is reused.
_CENTER_LON = -122.59
_CENTER_LAT = 45.37
_HALF_M = 300.0
_LOCK_NAME = "OR_PortlandMetro_B24"
# 0.30 of the accumulation max drew one channel thread here in Slice 0.
_FRACTION = 0.30


def _pass(label: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"PASS  {label}{suffix}")


def _fail(label: str, detail: str) -> None:
    print(f"FAIL  {label} - {detail}")


def _box():
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(_CENTER_LAT))
    half_lat = _HALF_M / meters_per_deg_lat
    half_lon = _HALF_M / meters_per_deg_lon
    return (
        _CENTER_LON - half_lon,
        _CENTER_LAT - half_lat,
        _CENTER_LON + half_lon,
        _CENTER_LAT + half_lat,
    )


def _provider():
    """3DEP client. Falls back to verify_ssl=False on the work network,
    same as dem.smoke, and only after a real SSL failure."""
    verified = create_provider("3dep", timeout=180)
    try:
        verified.session.get(
            "https://elevation.nationalmap.gov/arcgis/rest/services/"
            "3DEPElevation/ImageServer",
            params={"f": "json"},
            timeout=30,
            verify=True,
        )
        return verified, True
    except requests.exceptions.SSLError:
        print(
            "SSL verify failed. Work-network interception. "
            "Smoke is continuing with verify_ssl=False."
        )
        return create_provider("3dep", timeout=180, verify_ssl=False), False
    except requests.RequestException as exc:
        print(f"Probe did not answer ({exc}). Continuing.")
        return verified, True


def _lock_key(provider, bbox):
    catalog = list_collections(bbox, provider=provider)
    for item in catalog.collections:
        if item.name == _LOCK_NAME:
            return item.key
    print(f"{_LOCK_NAME} not in the catalog. Using suggested {catalog.suggested_key}.")
    return catalog.suggested_key


def _stream_cells_on_disk(path) -> int:
    with Image.open(path) as image:
        if image.mode != "F":
            raise ValueError(f"streams mode {image.mode}, expected F")
        values = np.array(image, dtype=np.float32)
    return int(np.count_nonzero(values > 0))


def run() -> int:
    failures = 0
    out_dir = Path(tempfile.gettempdir()) / "tributary-terrain-smoke"
    prep_dir = out_dir / "prep"
    if prep_dir.is_dir():
        shutil.rmtree(prep_dir)
    prep_dir.mkdir(parents=True, exist_ok=True)

    print("Terrain prep smoke")
    print("-" * 40)

    bbox = _box()
    provider, verify_ssl = _provider()
    params = PrepParams(accum_fraction=_FRACTION)

    label = "first prepare_terrain runs Whitebox"
    first = None
    try:
        key = _lock_key(provider, bbox)
        aoi = TerrainAoi(
            bbox=bbox,
            resolution_m=1,
            collection_key=key,
            verify_ssl=verify_ssl,
        )
        started = time.perf_counter()
        first = prepare_terrain(aoi, params, cache_dir=prep_dir, provider=provider, timeout=180)
        seconds = time.perf_counter() - started
        on_disk = _stream_cells_on_disk(first.streams_path)
        problems = []
        if first.from_cache:
            problems.append("was already cached")
        if first.stream_cells <= 0:
            problems.append("no stream cells")
        if on_disk != first.stream_cells:
            problems.append(f"file has {on_disk} stream cells, result says {first.stream_cells}")
        if first.epsg != 26910:
            problems.append(f"epsg {first.epsg}")
        if abs(first.cell_size_m - 1.0) > 0.01:
            problems.append(f"cell {first.cell_size_m}")
        if problems:
            _fail(label, "; ".join(problems))
            failures += 1
        else:
            _pass(
                label,
                f"{first.columns} x {first.rows} cells, accum max "
                f"{first.accum_max:.0f}, threshold {first.accum_threshold:.0f} "
                f"({_FRACTION:g} of max), {first.stream_cells} stream cells, "
                f"{seconds:.1f} s",
            )
            print(f"      DEM {first.dem_path} ({first.dem_collection_name})")
            print(f"      prep {Path(first.streams_path).parent}")
            print(f"      whitebox {first.whitebox_version}")
    except Exception as exc:
        _fail(label, str(exc))
        failures += 1

    label = "second prepare_terrain is a cache hit"
    try:
        if first is None:
            raise ValueError("first run failed")
        started = time.perf_counter()
        second = prepare_terrain(aoi, params, cache_dir=prep_dir, provider=provider, timeout=180)
        seconds = time.perf_counter() - started
        if not second.from_cache:
            _fail(label, "missed the cache")
            failures += 1
        elif second.cache_key != first.cache_key or second.paths != first.paths:
            _fail(label, "different key or paths")
            failures += 1
        else:
            _pass(label, f"key {second.cache_key}, {seconds:.2f} s")
    except Exception as exc:
        _fail(label, str(exc))
        failures += 1

    label = "same DEM by dem_path is the same entry"
    try:
        if first is None:
            raise ValueError("first run failed")
        by_path = prepare_terrain(
            TerrainAoi(dem_path=first.dem_path), params, cache_dir=prep_dir
        )
        if not by_path.from_cache or by_path.cache_key != first.cache_key:
            _fail(label, f"from_cache={by_path.from_cache} key {by_path.cache_key}")
            failures += 1
        else:
            _pass(label, by_path.dem_identity)
    except Exception as exc:
        _fail(label, str(exc))
        failures += 1

    print("-" * 40)
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("All terrain smoke checks passed")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
