"""Offline checks for the DEM routes. No network.

The service functions are patched, so nothing here talks to
3DEP. The boundary route uses the real file reader.

Run from data-downloader:
    python -m unittest tests.test_api_dem
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

from fastapi import HTTPException

from api.dem import (
    DEM_RESPONSE_HEADERS,
    CollectionsBody,
    DownloadBody,
    EstimateBody,
    boundary,
    collections,
    download,
    estimate,
    providers,
)
from api.main import app
from dem.boundary import Boundary
from dem.models import DemCatalog, DemEstimate, DemResult
from tests.test_dem_service import _collection


BOX = [-122.60, 45.36, -122.58, 45.38]


def _square(west, south, east, north):
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


def _result(path, **overrides):
    result = DemResult(
        path=str(path),
        provider="3dep",
        requested_resolution_m=1.0,
        source_resolution_m=1.0,
        coarsest_resolution_m=10.31,
        epsg=26910,
        bbox_projected=(500000.0, 5000000.0, 500100.0, 5000100.0),
        vertical_unit="meters",
        source_description="Locked to OR_PortlandMetro_B24.",
        from_cache=False,
        collection_key="OR_PortlandMetro_B24",
        collection_name="3DEP national DEM (10 m)",
        collection_period="2024",
        fill_key=None,
        fill_name=None,
        fill_period=None,
        fill_used=False,
        nodata=None,
        clip_name=None,
    )
    return replace(result, **overrides)


class _Upload:
    """Just enough of UploadFile for the boundary route."""

    def __init__(self, name, data):
        self.filename = name
        self._data = data
        self._pos = 0

    async def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class _Req:
    def __init__(self, origin=""):
        self.headers = {"origin": origin}


class ProvidersTests(unittest.TestCase):
    def test_lists_3dep_without_building_one(self):
        payload = providers()
        row = next(item for item in payload["providers"] if item["key"] == "3dep")
        self.assertEqual(row["display_name"], "USGS 3DEP")
        self.assertIn("lidar", row["description"])


class CollectionsTests(unittest.TestCase):
    def test_returns_picker_rows_for_the_named_provider(self):
        catalog = DemCatalog(
            collections=(
                _collection("OR_PortlandMetro_B24", "OR_PortlandMetro_B24", True),
            ),
            suggested_key="OR_PortlandMetro_B24",
            coverage_spacing_m=50.0,
        )
        seen = {}

        def fake(bbox, provider=None, **kwargs):
            seen["bbox"] = bbox
            seen["key"] = provider.key
            seen["verify_ssl"] = provider.verify_ssl
            return catalog

        body = CollectionsBody(
            bbox=BOX,
            provider=" 3dep ",
            verify_ssl=False,
        )
        with patch("api.dem.list_collections", fake):
            payload = collections(body)

        self.assertEqual(seen["bbox"], tuple(BOX))
        self.assertEqual(seen["key"], "3dep")
        self.assertIs(seen["verify_ssl"], False)
        self.assertEqual(payload["suggested_key"], "OR_PortlandMetro_B24")
        self.assertEqual(payload["coverage_spacing_m"], 50.0)
        row = payload["collections"][0]
        self.assertEqual(row["key"], "OR_PortlandMetro_B24")
        self.assertEqual(row["period_label"], "2024")
        self.assertTrue(row["covers_all"])
        self.assertEqual(row["footprint"]["type"], "Polygon")

    def test_unknown_provider_is_400(self):
        body = CollectionsBody(bbox=BOX, provider="nope")
        with self.assertRaises(HTTPException) as caught:
            collections(body)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("nope", caught.exception.detail)


class EstimateTests(unittest.TestCase):
    def test_over_cap_is_a_flag(self):
        estimated = DemEstimate(
            provider="3dep",
            resolution_m=1.0,
            width_px=7954,
            height_px=7881,
            estimated_mb=251.0,
            n_requests=4,
            over_cap=True,
            source_resolution_m=1.0,
            coarsest_resolution_m=30.92,
        )
        seen = {}

        def fake(request, **kwargs):
            seen["request"] = request
            seen["kwargs"] = kwargs
            return estimated

        body = EstimateBody(
            bbox=BOX,
            resolution_m="best",
            collection_key=" OR_PortlandMetro_B24 ",
            fill_key="  ",
            verify_ssl=False,
            max_mb=250,
        )
        with patch("api.dem.estimate_dem", fake):
            payload = estimate(body)

        self.assertEqual(seen["request"].bbox, tuple(BOX))
        self.assertEqual(seen["request"].resolution_m, "best")
        self.assertEqual(seen["request"].provider, "3dep")
        self.assertEqual(seen["kwargs"]["collection_key"], "OR_PortlandMetro_B24")
        self.assertIsNone(seen["kwargs"]["fill_key"])
        self.assertIs(seen["kwargs"]["verify_ssl"], False)
        self.assertEqual(seen["kwargs"]["max_mb"], 250)
        self.assertTrue(payload["over_cap"])
        self.assertEqual(payload["n_requests"], 4)
        self.assertEqual(payload["width_px"], 7954)
        self.assertEqual(payload["source_resolution_m"], 1.0)

    def test_bad_resolution_never_calls_the_service(self):
        def fake(*args, **kwargs):
            raise AssertionError("estimate should not run")

        body = EstimateBody(bbox=BOX, resolution_m="nope")
        with patch("api.dem.estimate_dem", fake):
            with self.assertRaises(HTTPException) as caught:
                estimate(body)
        self.assertEqual(caught.exception.status_code, 400)

    def test_upstream_failure_is_502(self):
        import requests

        def fake(*args, **kwargs):
            raise requests.ConnectionError("ssl")

        body = EstimateBody(bbox=BOX)
        with patch("api.dem.estimate_dem", fake):
            with self.assertRaises(HTTPException) as caught:
                estimate(body)
        self.assertEqual(caught.exception.status_code, 502)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "out.tif"
        self.payload = b"II*\x00not-a-real-tiff"
        self.path.write_bytes(self.payload)

    def test_box_download_returns_the_tiff_and_headers(self):
        # The headers describe the file get_dem actually returned.
        # A blank collection_key on the way in means "no lock".
        result = _result(
            self.path,
            from_cache=True,
            requested_resolution_m=10.0,
            nodata=-999999.0,
            collection_key=None,
            collection_name=None,
            collection_period=None,
            source_description="Server mosaic.",
        )
        seen = {}

        def fake(request, **kwargs):
            seen["request"] = request
            seen["kwargs"] = kwargs
            return result

        body = DownloadBody(
            bbox=BOX,
            resolution_m=10,
            collection_key="",
            fill_key=None,
        )
        with patch("api.dem.get_dem", fake):
            response = download(body)

        self.assertEqual(seen["request"].resolution_m, 10.0)
        self.assertIsNone(seen["kwargs"]["collection_key"])
        self.assertIsNone(seen["kwargs"]["fill_key"])
        self.assertIsNone(seen["kwargs"]["clip"])
        self.assertEqual(seen["kwargs"]["max_mb"], 250)
        self.assertEqual(response.body, self.payload)
        self.assertEqual(response.media_type, "image/tiff")
        self.assertIn(
            'filename="dem_3dep_10m.tif"',
            response.headers["content-disposition"],
        )
        self.assertEqual(response.headers["x-dem-from-cache"], "true")
        self.assertEqual(response.headers["x-dem-epsg"], "26910")
        self.assertEqual(response.headers["x-dem-requested-resolution"], "10")
        self.assertEqual(response.headers["x-dem-nodata"], "-999999")
        self.assertEqual(response.headers["x-dem-fill-used"], "false")
        self.assertEqual(response.headers["x-dem-collection-name"], "")
        self.assertEqual(
            unquote(response.headers["x-dem-description"]),
            "Server mosaic.",
        )
        for name in DEM_RESPONSE_HEADERS:
            self.assertIn(name.lower(), response.headers)

    def test_clip_and_fill_are_passed_through(self):
        # A browser file input sometimes includes a folder. Only
        # the file name belongs on the result. The accent has to
        # survive the header (percent-encoded) and stay out of
        # the download filename, which is plain ascii.
        clip_name = "caf\u00e9.geojson"
        seen = {}

        def fake(request, **kwargs):
            seen["request"] = request
            seen["clip"] = kwargs["clip"]
            seen["fill_key"] = kwargs["fill_key"]
            seen["verify_ssl"] = kwargs["verify_ssl"]
            name = None if kwargs["clip"] is None else kwargs["clip"].name
            return _result(
                self.path,
                clip_name=name,
                fill_used=True,
                fill_key=kwargs["fill_key"],
                fill_name="n40w118",
            )

        body = DownloadBody(
            bbox=BOX,
            collection_key="OR_PortlandMetro_B24",
            fill_key="n40w118",
            verify_ssl=False,
            clip={
                "name": "C:\\fakepath\\" + clip_name,
                "footprint": _square(*BOX),
            },
        )
        with patch("api.dem.get_dem", fake):
            response = download(body)

        self.assertIsInstance(seen["clip"], Boundary)
        self.assertEqual(seen["clip"].name, clip_name)
        self.assertEqual(seen["fill_key"], "n40w118")
        self.assertEqual(seen["request"].resolution_m, "best")
        self.assertIs(seen["verify_ssl"], False)
        self.assertEqual(response.headers["x-dem-fill-used"], "true")
        self.assertEqual(unquote(response.headers["x-dem-clip-name"]), clip_name)
        disposition = response.headers["content-disposition"]
        self.assertTrue(disposition.isascii())
        self.assertNotIn("\u00e9", disposition)
        self.assertIn("OR_PortlandMetro_B24", disposition)

    def test_over_cap_is_400(self):
        def fake(*args, **kwargs):
            raise ValueError("DEM is 7954 x 7881 pixels, over the 250 MB cap.")

        body = DownloadBody(bbox=BOX, resolution_m=1)
        with patch("api.dem.get_dem", fake):
            with self.assertRaises(HTTPException) as caught:
                download(body)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("cap", caught.exception.detail)

    def test_missing_tiff_is_500(self):
        self.path.unlink()

        def fake(*args, **kwargs):
            return _result(self.path)

        body = DownloadBody(bbox=BOX)
        with patch("api.dem.get_dem", fake):
            with self.assertRaises(HTTPException) as caught:
                download(body)
        self.assertEqual(caught.exception.status_code, 500)

    def test_empty_outline_is_400_and_does_not_download(self):
        def fake(*args, **kwargs):
            raise AssertionError("get_dem should not run")

        body = DownloadBody(
            bbox=BOX,
            clip={
                "name": "empty.geojson",
                "footprint": {"type": "Polygon", "coordinates": []},
            },
        )
        with patch("api.dem.get_dem", fake):
            with self.assertRaises(HTTPException) as caught:
                download(body)
        self.assertEqual(caught.exception.status_code, 400)


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_geojson_returns_the_rectangle_and_footprint(self):
        payload = {
            "type": "Feature",
            "properties": {},
            "geometry": _square(*BOX),
        }
        raw = json.dumps(payload).encode("utf-8")
        found = await boundary(_Req("http://127.0.0.1:5173/"), _Upload("basin.geojson", raw))
        self.assertEqual(found["name"], "basin.geojson")
        self.assertEqual(found["bbox"], BOX)
        self.assertEqual(found["footprint"]["type"], "MultiPolygon")

    async def test_empty_file_is_400(self):
        with self.assertRaises(HTTPException) as caught:
            await boundary(_Req(), _Upload("basin.geojson", b""))
        self.assertEqual(caught.exception.status_code, 400)

    async def test_unknown_suffix_is_400(self):
        with self.assertRaises(HTTPException) as caught:
            await boundary(_Req(), _Upload("notes.txt", b"hello"))
        self.assertEqual(caught.exception.status_code, 400)

    async def test_other_origin_is_403(self):
        with self.assertRaises(HTTPException) as caught:
            await boundary(_Req("https://evil.example"), _Upload("basin.geojson", b"{}"))
        self.assertEqual(caught.exception.status_code, 403)

    async def test_oversize_is_413(self):
        with patch("api.dem._MAX_UPLOAD_BYTES", 8):
            with self.assertRaises(HTTPException) as caught:
                await boundary(_Req(), _Upload("basin.geojson", b"0123456789"))
        self.assertEqual(caught.exception.status_code, 413)


class AppWiringTests(unittest.TestCase):
    def test_routes_are_mounted(self):
        # Included routers are not flat entries on app.routes
        # in this FastAPI. The OpenAPI paths are the public check.
        # The gage download keeps its own URL. dem_download is a
        # separate name so the two don't share one lookup key.
        paths = app.openapi()["paths"]
        for path in (
            "/api/dem/providers",
            "/api/dem/collections",
            "/api/dem/estimate",
            "/api/dem/boundary",
            "/api/dem/download",
            "/api/sources/{key}/download",
        ):
            self.assertIn(path, paths)
        self.assertEqual(app.url_path_for("dem_download"), "/api/dem/download")
        self.assertEqual(
            app.url_path_for("download", key="usgs"),
            "/api/sources/usgs/download",
        )

    def test_dem_headers_are_exposed_to_the_browser(self):
        found = []
        for layer in app.user_middleware:
            if getattr(layer.cls, "__name__", "") == "CORSMiddleware":
                found.extend(layer.kwargs.get("expose_headers") or [])
        for name in DEM_RESPONSE_HEADERS:
            self.assertIn(name, found)
