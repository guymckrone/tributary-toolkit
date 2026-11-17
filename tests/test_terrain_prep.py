"""Offline checks for terrain prep. No network.

Run from data-downloader:
    python -m unittest tests.test_terrain_prep

The Whitebox tests need whitebox_workflows, so they only run in
datadownload-geo:
    conda run -n datadownload-geo python -m unittest tests.test_terrain_prep

In datadownload they skip, and the rest still run.
"""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from dem.composite import write_grid_tiff
from dem.models import DemEstimate, DemRequest, DemResult
from dem.providers.base import DemProvider
from terrain.cache import (
    PREP_FILES,
    SIDECAR,
    clear_cache,
    entry_dir,
    lookup_prep,
    prep_cache_key,
    store_prep,
)
from terrain.dem_input import file_identity, resolve_dem
from terrain.models import PrepParams, PrepResult, TerrainAoi
from terrain.service import prepare_terrain

HAS_WHITEBOX = importlib.util.find_spec("whitebox_workflows") is not None

_BOX = (-122.6, 45.3, -122.5, 45.4)
_ROWS, _COLS = 40, 30
_VALLEY_COL = 15


def write_valley_dem(path, nodata_tag=True):
    """40 x 30 cells, 1 m, EPSG 26910. A V-shaped valley draining south.

    Row 0 is north. Elevation drops 0.5 m per row toward the
    south edge and rises 0.3 m per column away from column 15, so
    every cell drains into column 15 and then off the south edge.
    The north-west corner is -999999, the DEM package's nodata.
    """
    rows, cols = np.mgrid[0:_ROWS, 0:_COLS]
    values = 100.0 - 0.5 * rows + 0.3 * np.abs(cols - _VALLEY_COL)
    values[0, 0] = -999999.0
    write_grid_tiff(
        values,
        path,
        26910,
        500000.0,
        5000000.0,
        1.0,
        nodata=-999999.0 if nodata_tag else None,
    )
    return Path(path)


def _fake_prep(folder: Path, key: str) -> PrepResult:
    """Tiny not-really-rasters in a key folder, for cache tests."""
    target = entry_dir(folder, key)
    target.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, file_name in PREP_FILES.items():
        path = target / file_name
        path.write_bytes(b"II*\x00" + b"\x00" * 16)
        paths[name] = str(path)
    return PrepResult(
        cache_key=key,
        dem_path="C:/somewhere/dem.tif",
        dem_identity="sha256:0000",
        epsg=26910,
        cell_size_m=1.0,
        rows=_ROWS,
        columns=_COLS,
        accum_max=1199.0,
        accum_threshold=120.0,
        accum_fraction=0.1,
        stream_cells=31,
        breach_dist=50,
        fill_deps=False,
        nodata=-32768.0,
        whitebox_version="test",
        **paths,
    )


class PrepParamsTests(unittest.TestCase):
    def test_needs_exactly_one_threshold(self):
        with self.assertRaises(ValueError):
            PrepParams()
        with self.assertRaises(ValueError):
            PrepParams(accum_threshold=100, accum_fraction=0.1)

    def test_threshold_must_be_positive(self):
        with self.assertRaises(ValueError):
            PrepParams(accum_threshold=0)

    def test_fraction_must_be_zero_to_one(self):
        with self.assertRaises(ValueError):
            PrepParams(accum_fraction=0)
        with self.assertRaises(ValueError):
            PrepParams(accum_fraction=1.5)
        self.assertEqual(PrepParams(accum_fraction=1).accum_fraction, 1.0)

    def test_breach_dist_is_whole_positive_cells(self):
        with self.assertRaises(ValueError):
            PrepParams(accum_threshold=10, breach_dist=0)
        with self.assertRaises(ValueError):
            PrepParams(accum_threshold=10, breach_dist=2.5)

    def test_mainstem_only_is_refused_for_now(self):
        with self.assertRaises(ValueError) as caught:
            PrepParams(accum_threshold=10, mainstem_only=True)
        self.assertIn("isn't built", str(caught.exception))


class TerrainAoiTests(unittest.TestCase):
    def test_needs_bbox_or_path(self):
        with self.assertRaises(ValueError):
            TerrainAoi()

    def test_refuses_bbox_and_path_together(self):
        with self.assertRaises(ValueError) as caught:
            TerrainAoi(bbox=_BOX, dem_path="dem.tif")
        self.assertIn("not both", str(caught.exception))

    def test_collection_needs_a_download(self):
        with self.assertRaises(ValueError):
            TerrainAoi(dem_path="dem.tif", collection_key="project:x|1.00")

    def test_bad_resolution_fails_on_the_way_in(self):
        with self.assertRaises(ValueError):
            TerrainAoi(bbox=_BOX, resolution_m=-1)

    def test_dem_request_matches_the_box(self):
        aoi = TerrainAoi(bbox=_BOX, resolution_m=1)
        self.assertEqual(aoi.dem_request().bbox, _BOX)
        self.assertEqual(aoi.dem_request().resolution_m, 1.0)


class CacheKeyTests(unittest.TestCase):
    def test_same_dem_same_params_same_key(self):
        a = prep_cache_key("sha256:abc", PrepParams(accum_threshold=100))
        b = prep_cache_key("sha256:abc", PrepParams(accum_threshold=100.0))
        self.assertEqual(a, b)

    def test_dem_and_each_param_change_the_key(self):
        base = prep_cache_key("sha256:abc", PrepParams(accum_threshold=100))
        others = [
            prep_cache_key("sha256:abd", PrepParams(accum_threshold=100)),
            prep_cache_key("sha256:abc", PrepParams(accum_threshold=101)),
            prep_cache_key("sha256:abc", PrepParams(accum_fraction=0.1)),
            prep_cache_key("sha256:abc", PrepParams(accum_threshold=100, breach_dist=100)),
            prep_cache_key("sha256:abc", PrepParams(accum_threshold=100, fill_deps=True)),
        ]
        self.assertEqual(len(set(others + [base])), len(others) + 1)


class CacheTests(unittest.TestCase):
    def test_store_then_lookup_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            stored = store_prep(directory, _fake_prep(directory, "k1"))
            found = lookup_prep(directory, "k1")
        self.assertFalse(stored.from_cache)
        self.assertTrue(found.from_cache)
        self.assertEqual(found.accum_threshold, 120.0)
        self.assertEqual(found.accum_fraction, 0.1)
        self.assertTrue(found.streams_path.endswith("streams.tif"))

    def test_folder_without_sidecar_is_a_miss(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            _fake_prep(directory, "k1")
            self.assertIsNone(lookup_prep(directory, "k1"))

    def test_missing_raster_is_a_miss(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            store_prep(directory, _fake_prep(directory, "k1"))
            (entry_dir(directory, "k1") / "streams.tif").unlink()
            self.assertIsNone(lookup_prep(directory, "k1"))

    def test_broken_sidecar_is_a_miss(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            store_prep(directory, _fake_prep(directory, "k1"))
            (entry_dir(directory, "k1") / SIDECAR).write_text("{", encoding="utf-8")
            self.assertIsNone(lookup_prep(directory, "k1"))

    def test_clear_cache_keeps_loose_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "README.md").write_text("keep", encoding="utf-8")
            store_prep(directory, _fake_prep(directory, "k1"))
            store_prep(directory, _fake_prep(directory, "k2"))
            self.assertEqual(clear_cache(directory), 2)
            self.assertTrue((directory / "README.md").is_file())


class _FakeProvider(DemProvider):
    """Stands in for 3DEP so resolve_dem can go through get_dem."""

    def __init__(self, dem_bytes: bytes):
        self.dem_bytes = dem_bytes
        self.fetches = 0

    @property
    def key(self) -> str:
        return "3dep"

    @property
    def display_name(self) -> str:
        return "fake"

    def available_resolutions(self, bbox):
        return [1.0]

    def estimate(self, request: DemRequest) -> DemEstimate:
        return DemEstimate(
            provider=self.key,
            resolution_m=1.0,
            width_px=_COLS,
            height_px=_ROWS,
            estimated_mb=0.01,
            source_resolution_m=1.0,
            coarsest_resolution_m=1.0,
        )

    def fetch(self, request: DemRequest, out_path: str) -> DemResult:
        self.fetches += 1
        Path(out_path).write_bytes(self.dem_bytes)
        return DemResult(
            path=out_path,
            provider=self.key,
            requested_resolution_m=1.0,
            source_resolution_m=1.0,
            coarsest_resolution_m=1.0,
            epsg=26910,
            bbox_projected=(500000.0, 4999960.0, 500030.0, 5000000.0),
            vertical_unit="m",
            source_description="fake 1 m lidar",
        )


class DemInputTests(unittest.TestCase):
    def test_same_bytes_same_identity_anywhere(self):
        with tempfile.TemporaryDirectory() as folder:
            first = write_valley_dem(Path(folder) / "a.tif")
            second = Path(folder) / "b.tif"
            shutil.copyfile(first, second)
            self.assertEqual(file_identity(first), file_identity(second))

    def test_missing_path_is_refused(self):
        aoi = TerrainAoi(dem_path="C:/no/such/dem.tif")
        with self.assertRaises(ValueError):
            resolve_dem(aoi)

    def test_non_tiff_path_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dem.tif"
            path.write_text("not a tiff", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                resolve_dem(TerrainAoi(dem_path=str(path)))
        self.assertIn("not a GeoTIFF", str(caught.exception))

    def test_bbox_goes_through_get_dem_and_its_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            dem_bytes = write_valley_dem(Path(folder) / "src.tif").read_bytes()
            fake = _FakeProvider(dem_bytes)
            aoi = TerrainAoi(bbox=_BOX, resolution_m=1)
            dem_cache = Path(folder) / "dem_cache"
            first = resolve_dem(aoi, dem_cache_dir=dem_cache, provider=fake)
            second = resolve_dem(aoi, dem_cache_dir=dem_cache, provider=fake)
        self.assertEqual(fake.fetches, 1)
        self.assertFalse(first.dem.from_cache)
        self.assertTrue(second.dem.from_cache)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(second.dem.source_description, "fake 1 m lidar")


class CacheHitWithoutWhiteboxTests(unittest.TestCase):
    def test_hit_returns_before_whitebox_is_needed(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            dem_path = write_valley_dem(directory / "dem.tif")
            params = PrepParams(accum_fraction=0.1)
            key = prep_cache_key(file_identity(dem_path), params)
            store_prep(directory / "prep", _fake_prep(directory / "prep", key))
            # wbe that would blow up if prep tried to run.
            result = prepare_terrain(
                TerrainAoi(dem_path=str(dem_path)),
                params,
                cache_dir=directory / "prep",
                wbe=object(),
            )
        self.assertTrue(result.from_cache)
        self.assertEqual(result.cache_key, key)


@unittest.skipUnless(HAS_WHITEBOX, "whitebox_workflows not installed (use datadownload-geo)")
class WhiteboxPrepTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.dem_path = write_valley_dem(self.folder / "valley.tif")
        self.cache_dir = self.folder / "prep"

    def tearDown(self):
        self._folder.cleanup()

    def _prep(self, params, dem_path=None):
        return prepare_terrain(
            TerrainAoi(dem_path=str(dem_path or self.dem_path)),
            params,
            cache_dir=self.cache_dir,
        )

    def test_second_call_is_a_cache_hit(self):
        params = PrepParams(accum_fraction=0.1)
        first = self._prep(params)
        second = self._prep(params)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(first.paths, second.paths)
        self.assertEqual(first.stream_cells, second.stream_cells)
        self.assertGreater(first.stream_cells, 0)

    def test_copy_of_the_same_dem_hits_the_same_entry(self):
        params = PrepParams(accum_fraction=0.1)
        first = self._prep(params)
        copy = self.folder / "copy.tif"
        shutil.copyfile(self.dem_path, copy)
        second = self._prep(params, dem_path=copy)
        self.assertTrue(second.from_cache)
        self.assertEqual(first.cache_key, second.cache_key)

    def test_streams_follow_the_valley(self):
        result = self._prep(PrepParams(accum_fraction=0.1))
        with Image.open(result.streams_path) as image:
            self.assertEqual(image.mode, "F")
            streams = np.array(image, dtype=np.float32)
        self.assertEqual(streams.shape, (_ROWS, _COLS))
        stream_cols = set(np.nonzero(streams > 0)[1].tolist())
        self.assertTrue(stream_cols)
        self.assertTrue(stream_cols <= {_VALLEY_COL - 1, _VALLEY_COL, _VALLEY_COL + 1})
        self.assertEqual(int(np.count_nonzero(streams > 0)), result.stream_cells)

    def test_grid_facts_come_from_the_dem(self):
        result = self._prep(PrepParams(accum_threshold=50))
        self.assertEqual(result.epsg, 26910)
        self.assertAlmostEqual(result.cell_size_m, 1.0)
        self.assertEqual((result.rows, result.columns), (_ROWS, _COLS))
        self.assertEqual(result.accum_threshold, 50.0)
        self.assertIsNone(result.accum_fraction)
        self.assertIsNone(result.dem_source)

    def test_fraction_resolves_against_the_max(self):
        result = self._prep(PrepParams(accum_fraction=0.25))
        self.assertGreater(result.accum_max, 1)
        self.assertAlmostEqual(result.accum_threshold, result.accum_max * 0.25)

    def test_dem_nodata_stays_nodata_in_streams(self):
        result = self._prep(PrepParams(accum_fraction=0.1))
        with Image.open(result.streams_path) as image:
            streams = np.array(image, dtype=np.float32)
        self.assertEqual(float(streams[0, 0]), result.nodata)
        self.assertEqual(result.nodata, -32768.0)

    def test_threshold_over_the_max_raises_and_caches_nothing(self):
        with self.assertRaises(ValueError) as caught:
            self._prep(PrepParams(accum_threshold=1e9))
        self.assertIn("accumulation max", str(caught.exception))
        leftovers = [p for p in self.cache_dir.iterdir()] if self.cache_dir.is_dir() else []
        self.assertEqual(leftovers, [])

    def test_untagged_sentinel_is_refused(self):
        untagged = write_valley_dem(self.folder / "untagged.tif", nodata_tag=False)
        with self.assertRaises(ValueError) as caught:
            self._prep(PrepParams(accum_fraction=0.1), dem_path=untagged)
        self.assertIn("nodata tag", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
