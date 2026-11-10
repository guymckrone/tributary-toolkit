"""Offline checks for an uploaded outline. No network.

Run from data-downloader:
    python -m unittest tests.test_dem_boundary
"""

import io
import json
import struct
import tempfile
import unittest
import zipfile

import numpy as np

from dem.boundary import read_boundary
from dem.cache import cache_key
from dem.composite import punch_outside_file, write_grid_tiff, NODATA, read_float_tiff
from dem.models import DemRequest
from dem.providers.threedep import pixel_grid
from dem.service import get_dem
from tests.test_dem_service import _FakeProvider


SQUARE = [
    [-122.50, 45.30],
    [-122.40, 45.30],
    [-122.40, 45.40],
    [-122.50, 45.40],
    [-122.50, 45.30],
]


def _square_shp(ring):
    n_points = len(ring)
    content = struct.pack("<i", 5)
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    content += struct.pack("<4d", min(xs), min(ys), max(xs), max(ys))
    content += struct.pack("<2i", 1, n_points)
    content += struct.pack("<i", 0)
    for lon, lat in ring:
        content += struct.pack("<2d", lon, lat)
    words = len(content) // 2
    record = struct.pack(">2i", 1, words) + content
    header = bytearray(100)
    struct.pack_into(">i", header, 0, 9994)
    struct.pack_into(">i", header, 24, (100 + len(record)) // 2)
    struct.pack_into("<i", header, 28, 1000)
    struct.pack_into("<i", header, 32, 5)
    struct.pack_into("<4d", header, 36, min(xs), min(ys), max(xs), max(ys))
    return bytes(header) + record


def _zip(files):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return raw.getvalue()


class ReadBoundaryTests(unittest.TestCase):
    def test_geojson_square_becomes_a_rectangle_and_a_polygon(self):
        payload = {"type": "Polygon", "coordinates": [SQUARE]}
        boundary = read_boundary(
            "basin.geojson",
            json.dumps(payload).encode("utf-8"),
        )
        self.assertEqual(boundary.bbox, (-122.50, 45.30, -122.40, 45.40))
        self.assertEqual(boundary.footprint["type"], "MultiPolygon")
        self.assertEqual(boundary.name, "basin.geojson")

    def test_kml_and_kmz_read_the_same_ring(self):
        kml = """<?xml version="1.0"?>
        <kml><Document><Placemark><Polygon><outerBoundaryIs><LinearRing>
        <coordinates>-122.50,45.30 -122.40,45.30 -122.40,45.40 -122.50,45.40 -122.50,45.30</coordinates>
        </LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>
        """
        from_kml = read_boundary("basin.kml", kml.encode("utf-8"))
        kmz = _zip({"doc.kml": kml.encode("utf-8")})
        from_kmz = read_boundary("basin.kmz", kmz)
        self.assertEqual(from_kml.bbox, from_kmz.bbox)
        self.assertEqual(from_kml.cache_token(), from_kmz.cache_token())

    def test_shapefile_zip_reads_the_polygon(self):
        shp = _square_shp(SQUARE)
        blob = _zip({
            "basin.shp": shp,
            "basin.prj": 'GEOGCS["WGS 84"]',
        })
        boundary = read_boundary("basin.zip", blob)
        self.assertEqual(boundary.bbox[0], -122.50)
        self.assertEqual(boundary.bbox[2], -122.40)
        self.assertIn("basin.shp", boundary.name)

    def test_projected_shapefile_is_rejected(self):
        blob = _zip({
            "basin.shp": _square_shp(SQUARE),
            "basin.prj": 'PROJCS["State Plane"]',
        })
        with self.assertRaises(ValueError) as caught:
            read_boundary("basin.zip", blob)
        self.assertIn("projected", str(caught.exception).lower())

    def test_a_point_file_is_rejected(self):
        payload = {"type": "Point", "coordinates": [-122.5, 45.3]}
        with self.assertRaises(ValueError) as caught:
            read_boundary("site.geojson", json.dumps(payload).encode("utf-8"))
        self.assertIn("polygon", str(caught.exception).lower())

    def test_clip_changes_the_cache_key(self):
        boundary = read_boundary(
            "basin.geojson",
            json.dumps({"type": "Polygon", "coordinates": [SQUARE]}).encode("utf-8"),
        )
        request = DemRequest(bbox=boundary.bbox, resolution_m=10)
        plain = cache_key(request)
        clipped = cache_key(request, clip_token=boundary.cache_token())
        self.assertNotEqual(plain, clipped)


class PunchTests(unittest.TestCase):
    def test_corners_outside_the_polygon_become_nodata(self):
        # The polygon covers the west half of this box. East pixels
        # are inside the rectangle the server would download, and
        # outside the outline.
        grid = pixel_grid((-122.495, 45.305, -122.485, 45.315), 30)
        values = np.full((grid.height_px, grid.width_px), 12.0, dtype=np.float32)
        footprint = {"type": "Polygon", "coordinates": [[
            [-122.50, 45.30],
            [-122.49, 45.30],
            [-122.49, 45.32],
            [-122.50, 45.32],
            [-122.50, 45.30],
        ]]}
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "grid.tif"
            write_grid_tiff(
                values, path, epsg=grid.epsg,
                xmin=grid.xmin, ymax=grid.ymax,
                resolution_m=grid.resolution_m,
            )
            punch_outside_file(
                path, footprint, grid.epsg,
                grid.xmin, grid.ymax, grid.resolution_m,
            )
            punched, _image = read_float_tiff(path.read_bytes())
        self.assertEqual(float(punched[0, 0]), 12.0)
        self.assertEqual(float(punched[0, -1]), NODATA)


class ClipServiceTests(unittest.TestCase):
    def test_an_outline_off_to_the_side_does_not_download(self):
        fake = _FakeProvider(mb=1)
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m=10,
        )
        elsewhere = {
            "type": "Polygon",
            "coordinates": [[
                [10.0, 0.0], [11.0, 0.0], [11.0, 1.0], [10.0, 1.0], [10.0, 0.0],
            ]],
        }
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as caught:
                get_dem(request, provider=fake, cache_dir=folder, clip=elsewhere)
        self.assertIn("does not overlap", str(caught.exception))
        self.assertEqual(fake.fetches, 0)


if __name__ == "__main__":
    unittest.main()
