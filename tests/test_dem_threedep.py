"""Offline checks for the 3DEP provider. No network.

Run from data-downloader:
    python -m unittest tests.test_dem_threedep
"""

import unittest

from dem.models import DemCollection, DemRequest
from dem.providers.threedep import (
    coverage_from_payload,
    describe_coverage,
    mosaic_rule_for,
    pixel_grid,
)
from dem.registry import create_provider, get_descriptor, list_providers


def _feature(name, lowps, best, datum="NAVD88"):
    return {
        "attributes": {
            "Name": name,
            "LowPS": lowps,
            "Best": best,
            "VerticalDatum": datum,
        }
    }


class CatalogParseTests(unittest.TestCase):
    def test_finest_first_and_rounded(self):
        coverage = coverage_from_payload({
            "features": [
                _feature("ned10", 10.30736, 20),
                _feature("old-lidar", 1.004, 50),
                _feature("new-lidar", 1.0, 9),
                _feature("ned30", 30.922, 80),
            ]
        })
        # 1.004 and 1.0 are the same product at 2 decimals.
        self.assertEqual(coverage.resolutions_m, (1.0, 10.31, 30.92))
        # Same LowPS: the lower Best value wins the name.
        self.assertEqual(coverage.finest_name, "new-lidar")
        self.assertEqual(coverage.finest_m, 1.0)
        self.assertEqual(coverage.coarsest_m, 30.92)
        text = describe_coverage(coverage)
        self.assertIn("new-lidar", text)
        self.assertIn("30.92", text)

    def test_empty_catalog_is_an_error(self):
        with self.assertRaises(ValueError):
            coverage_from_payload({"features": []})

    def test_server_error_object_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            coverage_from_payload({
                "error": {"message": "Invalid geometry"}
            })
        self.assertIn("Invalid geometry", str(caught.exception))


class PixelGridTests(unittest.TestCase):
    def test_small_box_stays_under_the_server_limit(self):
        grid = pixel_grid((-122.59, 45.37, -122.58, 45.38), 10)
        self.assertEqual(grid.epsg, 26910)
        self.assertGreater(grid.width_px, 50)
        self.assertLess(grid.width_px, 8000)
        self.assertLess(grid.height_px, 8000)
        # Snapping makes extent / pixels equal the requested size.
        self.assertAlmostEqual(
            (grid.xmax - grid.xmin) / grid.width_px,
            10.0,
            places=6,
        )

    def test_one_meter_over_a_wide_box_is_a_grid_that_must_tile(self):
        # About 12 km on a side. At 1 m that is past one request.
        # The grid is still computed. The download splits it.
        grid = pixel_grid((-122.70, 45.30, -122.55, 45.40), 1)
        self.assertGreater(grid.width_px, 8000)
        self.assertGreater(grid.height_px, 8000)


class RegistryTests(unittest.TestCase):
    def test_catalog_lists_3dep_without_a_network_call(self):
        keys = [item.key for item in list_providers()]
        self.assertEqual(keys, ["3dep"])
        self.assertEqual(get_descriptor("3dep").display_name, "USGS 3DEP")

    def test_unknown_key_names_the_known_ones(self):
        with self.assertRaises(ValueError) as caught:
            create_provider("noaa")
        self.assertIn("3dep", str(caught.exception))

    def test_factory_honors_verify_ssl_and_does_not_fetch(self):
        provider = create_provider("3dep", verify_ssl=False, timeout=5)
        self.assertFalse(provider.verify_ssl)
        self.assertEqual(provider.timeout, 5)
        self.assertEqual(provider.key, "3dep")

    def test_mismatched_provider_on_a_request_raises_immediately(self):
        provider = create_provider("3dep")
        request = DemRequest(
            bbox=(-122.59, 45.37, -122.58, 45.38),
            resolution_m=10,
            provider="other",
        )
        with self.assertRaises(ValueError):
            provider.estimate(request)


class MosaicRuleTests(unittest.TestCase):
    def test_name_and_cell_size_keep_10_m_off_30_m(self):
        collection = DemCollection(
            key="national:10.31",
            name="3DEP national DEM (10 m)",
            kind="national",
            resolution_m=10.31,
            year=2024,
            start_text="1951",
            end_text="2024",
            acquisition_year=2024,
            coverage_pct=100.0,
            covers_all=True,
            vertical_datum="NAVD88",
            dem_type=1,
            tile_count=1,
            tile_names=("n40w118",),
            footprint=None,
        )
        rule = mosaic_rule_for(collection)
        where = rule["where"]
        self.assertIn("Name IN ('n40w118')", where)
        self.assertIn("LowPS >= 10.11", where)
        self.assertIn("LowPS <= 10.51", where)
        self.assertNotIn("30.92", where)

    def test_a_name_with_a_space_is_refused(self):
        collection = DemCollection(
            key="project:bad|1.00",
            name="bad name",
            kind="project",
            resolution_m=1.0,
            year=2024,
            start_text="2024",
            end_text="2024",
            acquisition_year=2024,
            coverage_pct=100.0,
            covers_all=True,
            vertical_datum="NAVD88",
            dem_type=1,
            tile_count=1,
            tile_names=("has a space",),
            footprint=None,
        )
        with self.assertRaises(ValueError):
            mosaic_rule_for(collection)


if __name__ == "__main__":
    unittest.main()
