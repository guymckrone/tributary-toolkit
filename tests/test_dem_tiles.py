"""Offline checks for splitting a DEM grid. No network.

Run from data-downloader:
    python -m unittest tests.test_dem_tiles
"""

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from dem.composite import read_float_tiff, write_grid_tiff
from dem.providers.threedep import pixel_grid
from dem.tiles import crop_to_core, request_bounds, tile_windows


class TileWindowTests(unittest.TestCase):
    def test_a_grid_that_fits_is_one_piece_without_a_halo(self):
        windows = tile_windows(100, 40, max_pixels=8000)
        self.assertEqual(len(windows), 1)
        only = windows[0]
        self.assertEqual((only.col0, only.row0, only.col1, only.row1), (0, 0, 100, 40))
        self.assertEqual(only.request_width, 100)
        self.assertEqual(only.request_height, 40)

    def test_cores_cover_the_grid_once_and_requests_fit(self):
        width, height = 30, 20
        windows = tile_windows(width, height, max_pixels=12)
        self.assertGreater(len(windows), 1)
        covered = np.zeros((height, width), dtype=int)
        for window in windows:
            self.assertLessEqual(window.request_width, 12)
            self.assertLessEqual(window.request_height, 12)
            covered[window.row0:window.row1, window.col0:window.col1] += 1
        self.assertEqual(int(covered.min()), 1)
        self.assertEqual(int(covered.max()), 1)

    def test_cropping_the_halo_rebuilds_the_original_grid(self):
        # Value at each pixel is its index, so a shifted crop is obvious.
        full = np.arange(20 * 30, dtype=np.float32).reshape(20, 30)
        windows = tile_windows(30, 20, max_pixels=12)
        out = np.empty_like(full)
        for window in windows:
            halo = full[
                window.row0_h:window.row1_h,
                window.col0_h:window.col1_h,
            ]
            core = crop_to_core(halo, window)
            out[window.row0:window.row1, window.col0:window.col1] = core
        self.assertTrue(np.array_equal(out, full))

    def test_request_edges_meet_and_the_halo_overlaps(self):
        windows = tile_windows(30, 10, max_pixels=12)
        # One row of pieces. Sort by column.
        row = sorted(
            (window for window in windows if window.row0 == 0),
            key=lambda window: window.col0,
        )
        self.assertGreater(len(row), 1)
        xmin, ymax, resolution = 500000.0, 5050000.0, 2.0
        previous = None
        for window in row:
            bounds = request_bounds(xmin, ymax, resolution, window)
            if previous is not None:
                # Cores share an edge. Requests overlap by the halo.
                self.assertEqual(previous.col1, window.col0)
                _prev = request_bounds(xmin, ymax, resolution, previous)
                self.assertGreater(_prev[2], bounds[0])
            previous = window
        # The outer request edges are the full grid.
        first = request_bounds(xmin, ymax, resolution, row[0])
        last = request_bounds(xmin, ymax, resolution, row[-1])
        self.assertEqual(first[0], xmin)
        self.assertEqual(last[2], xmin + 30 * resolution)


class MaskSplitTests(unittest.TestCase):
    def test_half_masks_match_the_mask_of_the_whole_grid(self):
        from dem.composite import footprint_mask

        ring = [
            (-122.50, 45.30),
            (-122.49, 45.30),
            (-122.49, 45.32),
            (-122.50, 45.32),
            (-122.50, 45.30),
        ]
        footprint = {"type": "Polygon", "coordinates": [ring]}
        grid = pixel_grid((-122.495, 45.305, -122.485, 45.315), 20)
        full = footprint_mask(
            footprint,
            grid.epsg - 26900,
            grid.xmin,
            grid.ymax,
            grid.width_px,
            grid.height_px,
            grid.resolution_m,
        )
        windows = tile_windows(grid.width_px, grid.height_px, max_pixels=15)
        self.assertGreater(len(windows), 1)
        parts = np.zeros_like(full)
        for window in windows:
            xmin = grid.xmin + window.col0 * grid.resolution_m
            ymax = grid.ymax - window.row0 * grid.resolution_m
            part = footprint_mask(
                footprint,
                grid.epsg - 26900,
                xmin,
                ymax,
                window.core_width,
                window.core_height,
                grid.resolution_m,
            )
            parts[window.row0:window.row1, window.col0:window.col1] = part
        self.assertTrue(np.array_equal(parts, full))
        # The polygon actually crosses this box, so the test is
        # not comparing two empty masks.
        self.assertGreater(int(full.sum()), 0)
        self.assertLess(int(full.sum()), full.size)


class GridTiffTests(unittest.TestCase):
    def test_stitched_file_carries_the_full_grid(self):
        values = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stitched.tif"
            write_grid_tiff(
                values,
                path,
                epsg=26910,
                xmin=500000.0,
                ymax=5050000.0,
                resolution_m=2.0,
                nodata=-999999.0,
            )
            raw = path.read_bytes()
        self.assertEqual(raw[:4], b"II*\x00")
        again, _image = read_float_tiff(raw)
        self.assertEqual(float(again[1, 1]), 4.0)
        image = Image.open(__import__("io").BytesIO(raw))
        scale = image.tag_v2.get(33550)
        tie = image.tag_v2.get(33922)
        self.assertAlmostEqual(float(scale[0]), 2.0)
        self.assertAlmostEqual(float(tie[3]), 500000.0)
        self.assertAlmostEqual(float(tie[4]), 5050000.0)
        keys = image.tag_v2.get(34735)
        self.assertIn(26910, [int(value) for value in keys])


if __name__ == "__main__":
    unittest.main()
