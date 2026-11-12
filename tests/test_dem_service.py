"""Offline checks for the DEM cache and get_dem. No network.

Run from data-downloader:
    python -m unittest tests.test_dem_service
"""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from dem.aoi import estimate_mb
from dem.cache import cache_key, clear_cache, lookup
from dem.composite import write_grid_tiff
from dem.models import (
    DemCatalog,
    DemCollection,
    DemEstimate,
    DemRequest,
    DemResult,
)
from dem.providers.base import DemProvider
from dem.providers.threedep import pixel_grid
from dem.service import DEFAULT_MAX_MB, estimate, get_dem


def over_cap_bbox():
    """About 7.9 km near Portland.

    At 1 m this is over the 250 MB cap and at most 8000 px, so
    the cap fires instead of the single-request pixel limit.
    """
    lat, lon = 45.37, -122.59
    dlat = 7850 / 111320
    dlon = 7900 / (111320 * 0.7023)
    return (lon, lat, lon + dlon, lat + dlat)


class _FakeProvider(DemProvider):
    """Stands in for 3DEP. Counts calls so a test can see a skip."""

    def __init__(self, mb: float):
        self.mb = mb
        self.estimates = 0
        self.fetches = 0

    @property
    def key(self) -> str:
        return "3dep"

    @property
    def display_name(self) -> str:
        return "fake"

    def available_resolutions(self, bbox):
        return [1.0, 10.0]

    def estimate(self, request: DemRequest) -> DemEstimate:
        self.estimates += 1
        if request.resolution_m == "best":
            resolution = 1.0
        else:
            resolution = float(request.resolution_m)
        return DemEstimate(
            provider=self.key,
            resolution_m=resolution,
            width_px=10,
            height_px=10,
            estimated_mb=self.mb,
            source_resolution_m=1.0,
            coarsest_resolution_m=10.0,
        )

    def fetch(self, request: DemRequest, out_path: str) -> DemResult:
        self.fetches += 1
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"II*\x00" + b"\x00" * 32)
        resolution = (
            1.0 if request.resolution_m == "best"
            else float(request.resolution_m)
        )
        return DemResult(
            path=out_path,
            provider=self.key,
            requested_resolution_m=resolution,
            source_resolution_m=1.0,
            coarsest_resolution_m=10.0,
            epsg=26910,
            bbox_projected=(1.0, 2.0, 3.0, 4.0),
            vertical_unit="m",
            source_description="fake source",
        )


class CacheKeyTests(unittest.TestCase):
    def test_one_and_one_point_zero_match(self):
        box = (-122.6, 45.3, -122.5, 45.4)
        a = DemRequest(bbox=box, resolution_m=1)
        b = DemRequest(bbox=box, resolution_m=1.0)
        self.assertEqual(cache_key(a), cache_key(b))

    def test_best_does_not_match_one_meter(self):
        box = (-122.6, 45.3, -122.5, 45.4)
        a = DemRequest(bbox=box, resolution_m="best")
        b = DemRequest(bbox=box, resolution_m=1)
        self.assertNotEqual(cache_key(a), cache_key(b))

    def test_seventh_decimal_does_not_change_the_key(self):
        a = DemRequest(bbox=(-122.5000001, 45.3, -122.4, 45.4), resolution_m=10)
        b = DemRequest(bbox=(-122.5000004, 45.3, -122.4, 45.4), resolution_m=10)
        self.assertEqual(cache_key(a), cache_key(b))

    def test_a_collection_key_does_not_hit_the_mosaic_cache(self):
        box = (-122.6, 45.3, -122.5, 45.4)
        request = DemRequest(bbox=box, resolution_m=1)
        plain = cache_key(request)
        locked = cache_key(request, collection_key="project:OR_PortlandMetro_B24|1.00")
        filled = cache_key(
            request,
            collection_key="project:OR_PortlandMetro_B24|1.00",
            fill_key="national:10.31",
        )
        self.assertNotEqual(plain, locked)
        self.assertNotEqual(locked, filled)


def _collection(key, name, covers_all, resolution_m=1.0):
    square = {
        "type": "Polygon",
        "coordinates": [[
            [-122.7, 45.2],
            [-122.3, 45.2],
            [-122.3, 45.6],
            [-122.7, 45.6],
            [-122.7, 45.2],
        ]],
    }
    return DemCollection(
        key=key,
        name=name,
        kind="project",
        resolution_m=resolution_m,
        year=2024,
        start_text="2024",
        end_text="2024",
        acquisition_year=2024,
        coverage_pct=100.0 if covers_all else 40.0,
        covers_all=covers_all,
        vertical_datum="NAVD88",
        dem_type=1,
        tile_count=1,
        tile_names=(name,),
        footprint=square,
    )


class _LockProvider(_FakeProvider):
    """Catalog plus a locked fetch. Records whether a fill was passed."""

    def __init__(self, mb, collections):
        super().__init__(mb)
        self.collections = collections
        self.locked_fills = []

    def list_collections(self, bbox):
        return DemCatalog(
            collections=tuple(self.collections),
            suggested_key=self.collections[0].key,
            coverage_spacing_m=20.0,
        )

    def locked_estimate(self, request, resolution_m, primary, fill=None):
        self.estimates += 1
        return DemEstimate(
            provider=self.key,
            resolution_m=float(resolution_m),
            width_px=10,
            height_px=10,
            estimated_mb=self.mb,
            n_requests=1 if fill is None else 2,
            source_resolution_m=primary.resolution_m,
            coarsest_resolution_m=(
                primary.resolution_m if fill is None else fill.resolution_m
            ),
        )

    def fetch_locked(self, request, out_path, primary, fill, resolution_m):
        self.fetches += 1
        self.locked_fills.append(None if fill is None else fill.key)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"II*\x00" + b"\x00" * 32)
        return DemResult(
            path=out_path,
            provider=self.key,
            requested_resolution_m=float(resolution_m),
            source_resolution_m=primary.resolution_m,
            coarsest_resolution_m=primary.resolution_m,
            epsg=26910,
            bbox_projected=(1.0, 2.0, 3.0, 4.0),
            vertical_unit="m",
            source_description="from the provider",
            collection_key=primary.key,
            collection_name=primary.name,
            collection_period=primary.period_label(),
            fill_key=None if fill is None else fill.key,
            fill_name=None if fill is None else fill.name,
            fill_period=None if fill is None else fill.period_label(),
            fill_used=fill is not None,
            nodata=-999999.0,
            zero_warning=getattr(self, "zero_warning", None),
            zero_warning_known=True,
        )


class LockedGetDemTests(unittest.TestCase):
    def _provider(self, mb=4, primary_covers=False):
        primary = _collection("project:lidar|1.00", "lidar", primary_covers, 1.0)
        fill = _collection("national:10.31", "national 10 m", True, 10.31)
        return _LockProvider(mb, [primary, fill]), primary, fill

    def test_second_locked_call_skips_the_provider(self):
        fake, primary, _fill = self._provider()
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m="best",
        )
        with tempfile.TemporaryDirectory() as folder:
            first = get_dem(
                request,
                provider=fake,
                cache_dir=folder,
                collection_key=primary.key,
            )
            second = get_dem(
                request,
                provider=fake,
                cache_dir=folder,
                collection_key=primary.key,
            )
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.collection_name, "lidar")
        self.assertEqual(second.collection_period, "2024")
        self.assertIsNone(second.fill_key)
        self.assertIn("No fill", second.source_description)
        self.assertEqual(first.requested_resolution_m, 1.0)
        self.assertEqual(fake.fetches, 1)
        self.assertEqual(fake.locked_fills, [None])
        self.assertIsNone(second.zero_warning)

    def test_zero_warning_survives_the_cache(self):
        fake, primary, _fill = self._provider()
        fake.zero_warning = (
            "Warning: every cell inside this flight is 0. "
            "3DEP has no elevation here."
        )
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m="best",
        )
        with tempfile.TemporaryDirectory() as folder:
            first = get_dem(
                request,
                provider=fake,
                cache_dir=folder,
                collection_key=primary.key,
            )
            second = get_dem(
                request,
                provider=fake,
                cache_dir=folder,
                collection_key=primary.key,
            )
        self.assertEqual(first.zero_warning, fake.zero_warning)
        self.assertEqual(second.zero_warning, fake.zero_warning)
        self.assertEqual(fake.fetches, 1)

    def test_full_primary_records_the_fill_but_does_not_download_it(self):
        fake, primary, fill = self._provider(primary_covers=True)
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=1,
        )
        with tempfile.TemporaryDirectory() as folder:
            result = get_dem(
                request,
                provider=fake,
                cache_dir=folder,
                collection_key=primary.key,
                fill_key=fill.key,
            )
        self.assertEqual(fake.locked_fills, [None])
        self.assertEqual(result.fill_name, "national 10 m")
        self.assertFalse(result.fill_used)
        self.assertIn("was not downloaded", result.source_description)

    def test_unknown_key_does_not_fetch(self):
        fake, _primary, _fill = self._provider()
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=1,
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as caught:
                get_dem(
                    request,
                    provider=fake,
                    cache_dir=folder,
                    collection_key="project:missing|1.00",
                )
            self.assertEqual(list(Path(folder).glob("*.tif")), [])
        self.assertIn("missing", str(caught.exception))
        self.assertEqual(fake.fetches, 0)

    def test_same_key_for_fill_is_refused(self):
        fake, primary, _fill = self._provider()
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=1,
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as caught:
                get_dem(
                    request,
                    provider=fake,
                    cache_dir=folder,
                    collection_key=primary.key,
                    fill_key=primary.key,
                )
        self.assertIn("same dataset", str(caught.exception))
        self.assertEqual(fake.fetches, 0)

    def test_fill_without_a_collection_is_refused(self):
        fake, _primary, fill = self._provider()
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=1,
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as caught:
                get_dem(
                    request,
                    provider=fake,
                    cache_dir=folder,
                    fill_key=fill.key,
                )
        self.assertIn("collection_key", str(caught.exception))
        self.assertEqual(fake.fetches, 0)

    def test_locked_over_cap_does_not_fetch(self):
        fake, primary, _fill = self._provider(mb=300)
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=1,
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as caught:
                get_dem(
                    request,
                    max_mb=DEFAULT_MAX_MB,
                    provider=fake,
                    cache_dir=folder,
                    collection_key=primary.key,
                )
            self.assertEqual(list(Path(folder).glob("*.tif")), [])
        self.assertIn("300.0 MB", str(caught.exception))
        self.assertEqual(fake.fetches, 0)

    def test_old_sidecar_without_lock_fields_still_loads(self):
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=10,
        )
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            key = cache_key(request)
            payload = {
                "provider": "3dep",
                "requested_resolution_m": 10.0,
                "source_resolution_m": 1.0,
                "coarsest_resolution_m": 10.0,
                "epsg": 26910,
                "bbox_projected": [1, 2, 3, 4],
                "vertical_unit": "m",
                "source_description": "old mosaic",
            }
            (directory / f"{key}.json").write_text(
                __import__("json").dumps(payload), encoding="utf-8"
            )
            (directory / f"{key}.tif").write_bytes(b"II*\x00")
            found = lookup(request, directory)
        self.assertIsNotNone(found)
        self.assertEqual(found.source_description, "old mosaic")
        self.assertIsNone(found.collection_key)
        self.assertFalse(found.fill_used)
        self.assertIsNone(found.nodata)

    def test_old_locked_tiff_is_measured_for_zeros(self):
        # Sidecar from before the check: no zero_warning key.
        # The TIFF is a flat sheet of 0, so the next read says so
        # without calling the provider.
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=1,
        )
        collection_key = "project:lidar|1.00"
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            key = cache_key(request, collection_key)
            write_grid_tiff(
                np.zeros((2, 2), dtype=np.float32),
                directory / f"{key}.tif",
                epsg=26910,
                xmin=100.0,
                ymax=200.0,
                resolution_m=1.0,
                nodata=-999999.0,
            )
            payload = {
                "provider": "3dep",
                "requested_resolution_m": 1.0,
                "source_resolution_m": 1.0,
                "coarsest_resolution_m": 1.0,
                "epsg": 26910,
                "bbox_projected": [100.0, 198.0, 102.0, 200.0],
                "vertical_unit": "m",
                "source_description": "old lock",
                "collection_key": collection_key,
                "collection_name": "lidar",
                "nodata": -999999.0,
            }
            (directory / f"{key}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            found = get_dem(
                request,
                cache_dir=directory,
                collection_key=collection_key,
            )
        self.assertTrue(found.from_cache)
        self.assertIn("every cell inside", found.zero_warning)


class GetDemTests(unittest.TestCase):
    def test_second_call_is_the_cache_and_skips_the_provider(self):
        fake = _FakeProvider(mb=4)
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=10,
        )
        with tempfile.TemporaryDirectory() as folder:
            first = get_dem(request, provider=fake, cache_dir=folder)
            second = get_dem(request, provider=fake, cache_dir=folder)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(first.path, second.path)
        self.assertEqual(second.source_description, "fake source")
        self.assertEqual(fake.fetches, 1)
        self.assertEqual(fake.estimates, 1)

    def test_over_cap_raises_and_does_not_fetch(self):
        fake = _FakeProvider(mb=300)
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=1,
        )
        with tempfile.TemporaryDirectory() as folder:
            sized = estimate(request, max_mb=DEFAULT_MAX_MB, provider=fake)
            self.assertTrue(sized.over_cap)
            self.assertEqual(sized.estimated_mb, 300)
            with self.assertRaises(ValueError) as caught:
                get_dem(
                    request,
                    max_mb=DEFAULT_MAX_MB,
                    provider=fake,
                    cache_dir=folder,
                )
            self.assertEqual(list(Path(folder).glob("*.tif")), [])
        message = str(caught.exception)
        self.assertIn("300.0 MB", message)
        self.assertIn("250 MB", message)
        self.assertEqual(fake.fetches, 0)

    def test_clear_cache_forces_another_fetch(self):
        fake = _FakeProvider(mb=1)
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=30,
        )
        with tempfile.TemporaryDirectory() as folder:
            get_dem(request, provider=fake, cache_dir=folder)
            removed = clear_cache(Path(folder))
            self.assertGreaterEqual(removed, 2)
            again = get_dem(request, provider=fake, cache_dir=folder)
        self.assertFalse(again.from_cache)
        self.assertEqual(fake.fetches, 2)

    def test_broken_sidecar_is_a_miss(self):
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=10,
        )
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            key = cache_key(request)
            (directory / f"{key}.json").write_text("{", encoding="utf-8")
            (directory / f"{key}.tif").write_bytes(b"II*\x00")
            self.assertIsNone(lookup(request, directory))


class CapBoxTests(unittest.TestCase):
    def test_example_box_is_between_the_cap_and_the_pixel_limit(self):
        grid = pixel_grid(over_cap_bbox(), 1)
        mb = estimate_mb(grid.width_px, grid.height_px)
        self.assertGreater(mb, DEFAULT_MAX_MB)
        self.assertLessEqual(grid.width_px, 8000)
        self.assertLessEqual(grid.height_px, 8000)


if __name__ == "__main__":
    unittest.main()
