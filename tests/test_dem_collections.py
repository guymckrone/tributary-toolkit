"""Offline checks for the collection picker. No network.

Run from data-downloader:
    python -m unittest tests.test_dem_collections
"""

import unittest

from dem.collections import build_catalog, suggest_key


def _ring(west, south, east, north):
    return [[
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
    ]]


def _feature(name, lowps, rings, start=20200101, end=20201231, acq=None):
    return {
        "attributes": {
            "Name": name,
            "LowPS": lowps,
            "StartDate": start,
            "EndDate": end,
            "AcquisitionDate": acq,
            "VerticalDatum": "NAVD88",
            "DEM_Type": 1,
        },
        "geometry": {"rings": rings},
    }


BOX = (-122.6, 45.3, -122.4, 45.4)


class GroupTests(unittest.TestCase):
    def test_same_project_tiles_become_one_choice(self):
        features = [
            _feature("OR_Example_2020", 1, _ring(-122.6, 45.3, -122.5, 45.4)),
            _feature("OR_Example_2020", 1, _ring(-122.5, 45.3, -122.4, 45.4)),
        ]
        catalog = build_catalog(BOX, features)
        self.assertEqual(len(catalog.collections), 1)
        only = catalog.collections[0]
        self.assertEqual(only.tile_count, 2)
        self.assertEqual(only.kind, "project")
        self.assertGreaterEqual(only.coverage_pct, 95)
        self.assertTrue(only.covers_all)
        self.assertEqual(only.footprint["type"], "MultiPolygon")
        self.assertEqual(len(only.footprint["coordinates"]), 2)

    def test_national_resolutions_stay_separate(self):
        big = _ring(-123.0, 45.0, -122.0, 46.0)
        features = [
            _feature("n46w123", 10.307, big, end=20240809),
            _feature("n46w123", 30.922, big, end=20240809),
            _feature("n45w123", 10.307, big, end=20240809),
        ]
        catalog = build_catalog(BOX, features)
        kinds = {(item.kind, item.resolution_m) for item in catalog.collections}
        self.assertEqual(kinds, {("national", 10.31), ("national", 30.92)})
        ten = next(item for item in catalog.collections if item.resolution_m == 10.31)
        self.assertEqual(ten.tile_count, 2)
        self.assertEqual(ten.name, "3DEP national DEM (10 m)")
        self.assertEqual(ten.period_label(), "through 2024")
        self.assertTrue(ten.covers_all)

    def test_west_half_is_not_full_coverage(self):
        features = [
            _feature(
                "OR_WestOnly",
                1,
                _ring(-122.7, 45.2, -122.5, 45.5),
                start=20190301,
                end=20190901,
            )
        ]
        catalog = build_catalog(BOX, features)
        only = catalog.collections[0]
        self.assertFalse(only.covers_all)
        self.assertGreater(only.coverage_pct, 35)
        self.assertLess(only.coverage_pct, 65)
        self.assertEqual(only.period_label(), "2019")
        self.assertEqual(only.year, 2019)

    def test_multi_year_window_is_shown_as_a_span(self):
        features = [
            _feature(
                "ned19_example_2007",
                3.4,
                _ring(-123, 45, -122, 46),
                start=2009,
                end=2014,
            )
        ]
        only = build_catalog(BOX, features).collections[0]
        self.assertEqual(only.period_label(), "2009-2014")
        self.assertEqual(only.year, 2014)
        self.assertEqual(only.start_text, "2009")
        self.assertEqual(only.end_text, "2014")


class SuggestTests(unittest.TestCase):
    def _catalog(self, features):
        return build_catalog(BOX, features)

    def test_newer_full_lidar_beats_older_full_lidar(self):
        big = _ring(-123, 45, -122, 46)
        catalog = self._catalog([
            _feature("old", 1, big, start=20100101, end=20101231),
            _feature("new", 1, big, start=20240101, end=20241231),
        ])
        self.assertEqual(catalog.suggested_key, "project:new|1.00")

    def test_partial_lidar_beats_national_when_it_covers_most(self):
        # West 80% of the box: -122.6 to -122.44 is 0.16 of 0.20.
        catalog = self._catalog([
            _feature("partial", 1, _ring(-122.7, 45.2, -122.44, 45.5), end=20220101),
            _feature("n46w123", 10.3, _ring(-124, 44, -121, 47), end=20240101),
        ])
        self.assertEqual(catalog.suggested_key, "project:partial|1.00")

    def test_sliver_of_lidar_does_not_beat_full_national(self):
        catalog = self._catalog([
            _feature("sliver", 1, _ring(-122.7, 45.2, -122.58, 45.5), end=20240101),
            _feature("n46w123", 10.307, _ring(-124, 44, -121, 47), end=20200101),
            _feature("n46w123", 30.922, _ring(-124, 44, -121, 47), end=20200101),
        ])
        self.assertEqual(catalog.suggested_key, "national:10.31")

    def test_empty_catalog_suggests_nothing(self):
        self.assertIsNone(suggest_key(()))


if __name__ == "__main__":
    unittest.main()
