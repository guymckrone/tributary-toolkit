"""Offline checks for the locked-DEM stack. No network.

Run from data-downloader:
    python -m unittest tests.test_dem_composite
"""

import io
import unittest

import numpy as np
from PIL import Image
from PIL.TiffImagePlugin import ImageFileDirectory_v2

from dem.collections import _point_in_ring
from dem.composite import (
    NODATA,
    composite_arrays,
    footprint_mask,
    read_float_tiff,
    write_float_tiff,
)


class CompositeTests(unittest.TestCase):
    def test_primary_wins_and_fill_only_lands_in_holes(self):
        primary = np.array([[1.0, 5.0], [5.0, np.nan]], dtype=np.float32)
        primary_inside = np.array([[True, False], [True, True]])
        fill = np.array([[9.0, 9.0], [8.0, 7.0]], dtype=np.float32)
        fill_inside = np.array([[True, True], [True, False]])
        stacked = composite_arrays(primary, primary_inside, fill, fill_inside)
        # Overlap keeps the primary. The hole (top right) takes the
        # fill. The bottom right is inside the primary but not finite,
        # so the fill is not allowed in. Bottom left of the fill is
        # inside the primary, so 8 is dropped.
        self.assertEqual(float(stacked[0, 0]), 1.0)
        self.assertEqual(float(stacked[0, 1]), 9.0)
        self.assertEqual(float(stacked[1, 0]), 5.0)
        self.assertEqual(float(stacked[1, 1]), NODATA)

    def test_no_fill_leaves_holes_as_nodata(self):
        primary = np.array([[4.0, 4.0]], dtype=np.float32)
        inside = np.array([[True, False]])
        stacked = composite_arrays(primary, inside)
        self.assertEqual(float(stacked[0, 0]), 4.0)
        self.assertEqual(float(stacked[0, 1]), NODATA)


class FootprintMaskTests(unittest.TestCase):
    def test_matches_the_point_in_ring_helper(self):
        # A square in UTM-ish lon/lat. The mask and the catalog's
        # even-odd test have to agree, or coverage % and the holes
        # will disagree.
        ring = [
            (-122.50, 45.30),
            (-122.40, 45.30),
            (-122.40, 45.40),
            (-122.50, 45.40),
        ]
        footprint = {"type": "Polygon", "coordinates": [ring]}
        mask = footprint_mask(
            footprint,
            zone=10,
            xmin=500000.0,
            ymax=5050000.0,
            width=4,
            height=3,
            resolution_m=100.0,
        )
        self.assertEqual(mask.shape, (3, 4))
        for row in range(3):
            north = 5050000.0 - (row + 0.5) * 100.0
            for col in range(4):
                east = 500000.0 + (col + 0.5) * 100.0
                # Inverse of the hand UTM is not what I have. Compare
                # in lon/lat by projecting the ring the same way the
                # mask does, then testing the pixel center.
                from dem.aoi import lonlat_to_utm
                projected = [lonlat_to_utm(lon, lat, 10)[1:] for lon, lat in ring]
                expected = _point_in_ring(east, north, projected)
                self.assertEqual(bool(mask[row, col]), expected)

    def test_a_box_across_the_edge_is_split(self):
        # Polygon covers lon <= -122.49. The grid is wide enough that
        # the west column is inside and the east column is outside.
        ring = [
            (-122.50, 45.30),
            (-122.49, 45.30),
            (-122.49, 45.32),
            (-122.50, 45.32),
            (-122.50, 45.30),
        ]
        footprint = {"type": "Polygon", "coordinates": [ring]}
        from dem.providers.threedep import pixel_grid
        box = (-122.495, 45.305, -122.485, 45.315)
        grid = pixel_grid(box, 20)
        mask = footprint_mask(
            footprint,
            zone=grid.epsg - 26900,
            xmin=grid.xmin,
            ymax=grid.ymax,
            width=grid.width_px,
            height=grid.height_px,
            resolution_m=grid.resolution_m,
        )
        self.assertTrue(bool(mask[:, 0].all()))
        self.assertFalse(bool(mask[:, -1].any()))
        self.assertGreater(int(mask.sum()), 0)
        self.assertLess(int(mask.sum()), mask.size)


class GeoTiffRoundTripTests(unittest.TestCase):
    def test_rewrite_keeps_georef_and_sets_nodata(self):
        template = Image.fromarray(
            np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32), mode="F"
        )
        info = ImageFileDirectory_v2()
        info[33550] = (2.0, 2.0, 0.0)
        info[33922] = (0.0, 0.0, 0.0, 100.0, 200.0, 0.0)
        info[34735] = (1, 1, 0, 1, 3072, 0, 1, 26910)
        raw = io.BytesIO()
        template.save(raw, format="TIFF", tiffinfo=info)
        values, image = read_float_tiff(raw.getvalue())
        self.assertEqual(values.shape, (2, 2))
        changed = np.array([[1.0, NODATA], [3.0, 4.0]], dtype=np.float32)
        raw_out = io.BytesIO()
        # write_float_tiff wants a path. Use a temp name via a real file
        # so Pillow's TIFF writer can seek.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out.tif"
            write_float_tiff(image, changed, path)
            saved = path.read_bytes()
        self.assertEqual(saved[:4], b"II*\x00")
        again, _reread = read_float_tiff(saved)
        self.assertEqual(float(again[0, 0]), 1.0)
        self.assertEqual(float(again[0, 1]), NODATA)
        reread = Image.open(io.BytesIO(saved))
        scale = reread.tag_v2.get(33550)
        tie = reread.tag_v2.get(33922)
        nodata_tag = reread.tag_v2.get(42113)
        self.assertIsNotNone(scale)
        self.assertAlmostEqual(float(scale[0]), 2.0)
        self.assertIsNotNone(tie)
        self.assertAlmostEqual(float(tie[3]), 100.0)
        self.assertAlmostEqual(float(tie[4]), 200.0)
        text = nodata_tag.decode("ascii", "ignore") if isinstance(nodata_tag, bytes) else str(nodata_tag)
        self.assertIn("999999", text)
        self.assertIsNotNone(reread.tag_v2.get(34735))


if __name__ == "__main__":
    unittest.main()
