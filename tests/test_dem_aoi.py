"""Offline checks for dem.aoi. No network.

Run from data-downloader:
    python -m unittest tests.test_dem_aoi
"""

import unittest

from dem.aoi import (
    bbox_to_utm,
    epsg_nad83_utm,
    estimate_mb,
    lonlat_to_utm,
    snap_outward,
    utm_zone,
    validate_bbox,
)
from dem.models import DemRequest
from dem.providers.base import DemProvider


# Center of a lon/lat box the ImageServer projected to EPSG:26910
# on Nov 6, 2026. Our formula was about 1.2 m off. The test allows 2.
JOHNSON_CREEK_CENTER = (-122.5875, 45.3720, 532301.38, 5024358.39)


class BboxTests(unittest.TestCase):
    def test_accepts_a_normal_box(self):
        self.assertEqual(
            validate_bbox((-122.6, 45.3, -122.5, 45.4)),
            (-122.6, 45.3, -122.5, 45.4),
        )

    def test_rejects_flipped_and_empty(self):
        with self.assertRaises(ValueError):
            validate_bbox((-122.5, 45.3, -122.6, 45.4))
        with self.assertRaises(ValueError):
            validate_bbox((-122.5, 45.4, -122.4, 45.4))

    def test_rejects_projected_numbers(self):
        # State Plane-ish feet. Must not be sent to a lon/lat API.
        with self.assertRaises(ValueError):
            validate_bbox((7600000, 680000, 7610000, 690000))

    def test_rejects_short_tuple(self):
        with self.assertRaises(ValueError):
            validate_bbox((-122.6, 45.3, -122.5))


class UtmTests(unittest.TestCase):
    def test_zone_boundaries(self):
        self.assertEqual(utm_zone(-122.5), 10)
        # -120 is the west edge of zone 11.
        self.assertEqual(utm_zone(-120.0), 11)
        self.assertEqual(utm_zone(-117.0), 11)
        self.assertEqual(epsg_nad83_utm(10), 26910)
        self.assertEqual(epsg_nad83_utm(11), 26911)

    def test_central_meridian_easting_is_500000(self):
        # Zone 10's central meridian is -123. Easting there is
        # the false easting, exactly, if the series is right.
        zone, easting, northing = lonlat_to_utm(-123.0, 45.0)
        self.assertEqual(zone, 10)
        self.assertAlmostEqual(easting, 500000.0, places=2)
        self.assertGreater(northing, 4_000_000)

    def test_matches_imageserver_within_two_meters(self):
        lon, lat, east, north = JOHNSON_CREEK_CENTER
        zone, easting, northing = lonlat_to_utm(lon, lat)
        self.assertEqual(zone, 10)
        self.assertAlmostEqual(easting, east, delta=2.0)
        self.assertAlmostEqual(northing, north, delta=2.0)

    def test_southern_latitude_is_refused(self):
        with self.assertRaises(ValueError):
            lonlat_to_utm(-122.5, -33.0)

    def test_box_uses_the_center_zone(self):
        # Straddles the zone 10 / 11 boundary at -120.
        # Center is -120.05, which is still zone 10.
        zone, xmin, ymin, xmax, ymax = bbox_to_utm(
            (-120.2, 45.0, -119.9, 45.1)
        )
        self.assertEqual(zone, 10)
        self.assertLess(xmin, xmax)
        self.assertLess(ymin, ymax)

    def test_forced_zone_keeps_a_neighboring_point(self):
        zone, easting, _ = lonlat_to_utm(-119.9, 45.0, zone=10)
        self.assertEqual(zone, 10)
        # East of zone 10's boundary, so easting past the zone's
        # eastern edge (~833000 at this latitude) is expected.
        self.assertGreater(easting, 500000)


class SnapTests(unittest.TestCase):
    def test_grows_outward_to_whole_pixels(self):
        xmin, ymin, xmax, ymax, width, height = snap_outward(
            100.2, 200.2, 110.2, 220.8, 1
        )
        self.assertEqual((xmin, ymin, xmax, ymax), (100.0, 200.0, 111.0, 221.0))
        self.assertEqual((width, height), (11, 21))

    def test_aligned_box_does_not_grow(self):
        snapped = snap_outward(100.0, 200.0, 150.0, 260.0, 10)
        self.assertEqual(snapped, (100.0, 200.0, 150.0, 260.0, 5, 6))

    def test_ten_meter_pixels(self):
        _, _, _, _, width, height = snap_outward(
            100.2, 200.2, 110.2, 220.2, 10
        )
        self.assertEqual((width, height), (2, 3))

    def test_one_square_kilometer_at_one_meter_is_four_mb(self):
        # 1000 x 1000 float32 = 4,000,000 bytes = 4.0 decimal MB.
        # The plan's size table uses this number.
        self.assertEqual(estimate_mb(1000, 1000), 4.0)

    def test_bad_resolution_raises(self):
        with self.assertRaises(ValueError):
            snap_outward(0, 0, 10, 10, 0)


class RequestTests(unittest.TestCase):
    def test_best_is_allowed(self):
        request = DemRequest(bbox=(-122.6, 45.3, -122.5, 45.4))
        self.assertEqual(request.resolution_m, "best")
        self.assertEqual(request.provider, "3dep")

    def test_numeric_resolution_is_stored_as_float(self):
        request = DemRequest(
            bbox=(-122.6, 45.3, -122.5, 45.4),
            resolution_m="10",
        )
        self.assertEqual(request.resolution_m, 10.0)

    def test_zero_resolution_raises(self):
        with self.assertRaises(ValueError):
            DemRequest(
                bbox=(-122.6, 45.3, -122.5, 45.4),
                resolution_m=0,
            )

    def test_provider_cannot_be_constructed(self):
        with self.assertRaises(TypeError):
            DemProvider()


if __name__ == "__main__":
    unittest.main()
