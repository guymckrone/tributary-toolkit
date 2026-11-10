"""Live 3DEP checks. One small lidar box, one coarse-only box.

Run from data-downloader:

    python -m dem.smoke

Hits the public ImageServer. Not part of the Water Data quota.
Writes GeoTIFFs under the system temp folder.
"""

import io
import sys
import tempfile
from pathlib import Path

import requests
from PIL import Image

from .cache import cache_key
from .composite import NODATA
from .models import DemRequest
from .providers.threedep import pixel_grid
from .registry import create_provider
from .service import DEFAULT_MAX_MB, get_dem

# Johnson Creek. Catalog there is 1 m lidar over the national DEM.
_JC_LON = -122.5858
_JC_LAT = 45.3725

# Austin, Nevada. Catalog on Nov 6, 2026 was the 10 m national
# DEM only (n40w118). A 1 m request still returns pixels.
_AUSTIN_LON = -117.07
_AUSTIN_LAT = 39.49


def _pass(label: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"PASS  {label}{suffix}")


def _fail(label: str, detail: str) -> None:
    print(f"FAIL  {label} - {detail}")


def _box(lon: float, lat: float, half_deg: float):
    return (
        lon - half_deg,
        lat - half_deg,
        lon + half_deg,
        lat + half_deg,
    )


def _read_tiff(path: Path):
    raw = path.read_bytes()
    if raw[:4] != b"II*\x00":
        raise ValueError(f"not a TIFF: {raw[:20]!r}")
    image = Image.open(io.BytesIO(raw))
    scale = image.tag_v2.get(33550)
    return image, scale


def _provider():
    """3DEP client. Falls back to verify_ssl=False on this network.

    Nov 6 the ImageServer verified. On the work network the
    interceptor's certificate does not, same as Aquarius and
    SNOTEL. Default stays verified. Only this smoke relaxes it,
    and only after a real SSL failure.
    """
    verified = create_provider("3dep", timeout=120)
    try:
        verified.session.get(
            "https://elevation.nationalmap.gov/arcgis/rest/services/"
            "3DEPElevation/ImageServer",
            params={"f": "json"},
            timeout=30,
            verify=True,
        )
        return verified
    except requests.exceptions.SSLError:
        print(
            "SSL verify failed. Work-network interception. "
            "Smoke is continuing with verify_ssl=False."
        )
        return create_provider("3dep", timeout=120, verify_ssl=False)
    except requests.RequestException as exc:
        # A 502 here is the server, not the certificate. The
        # checks below already retry those.
        print(f"Probe did not answer ({exc}). Continuing.")
        return verified


def _is_hole(value: float) -> bool:
    return value != value or abs(value - NODATA) < 1.0


def _locked_johnson(provider, johnson, cache_dir) -> int:
    """The suggested flight, locked, still looks like Johnson Creek."""
    label = "locked Johnson Creek"
    try:
        catalog = provider.list_collections(johnson)
        key = catalog.suggested_key
        if not key:
            _fail(label, "catalog had no suggestion")
            return 1
        request = DemRequest(bbox=johnson, resolution_m="best")
        # A previous smoke leaves this file behind. Drop it so the
        # first call is a real download and the second is the hit.
        cache_dir.mkdir(parents=True, exist_ok=True)
        stale = cache_key(request, key)
        for suffix in (".tif", ".json", ".json.tmp"):
            leftover = cache_dir / f"{stale}{suffix}"
            if leftover.is_file():
                leftover.unlink()
        first = get_dem(
            request,
            cache_dir=cache_dir,
            provider=provider,
            timeout=180,
            collection_key=key,
        )
        second = get_dem(
            request,
            cache_dir=cache_dir,
            provider=provider,
            timeout=180,
            collection_key=key,
        )
        image, scale = _read_tiff(Path(first.path))
        pixels = list(image.get_flattened_data())
        ground = [value for value in pixels if not _is_hole(value)]
        problems = []
        if first.from_cache:
            problems.append("first call was already cached")
        if not second.from_cache:
            problems.append("second call missed the cache")
        if first.collection_key != key:
            problems.append(f"key {first.collection_key}")
        if not first.collection_name:
            problems.append("no collection name")
        if first.fill_used:
            problems.append("fill was used with no fill_key")
        if first.nodata != NODATA:
            problems.append(f"nodata {first.nodata}")
        if not ground:
            problems.append("no ground elevations")
        elif min(ground) < 0 or max(ground) > 400:
            problems.append(
                f"elevations {min(ground):.1f} to {max(ground):.1f} m"
            )
        if scale is None or abs(float(scale[0]) - first.requested_resolution_m) > 0.05:
            problems.append(f"pixel scale {scale}")
        tag = image.tag_v2.get(42113)
        tag_text = tag.decode("ascii", "ignore") if isinstance(tag, bytes) else str(tag)
        if "999999" not in tag_text:
            problems.append(f"nodata tag {tag!r}")
        if problems:
            _fail(label, "; ".join(problems))
            return 1
        _pass(
            label,
            f"{first.collection_name} ({first.collection_period}) "
            f"{min(ground):.1f}-{max(ground):.1f} m",
        )
        return 0
    except Exception as exc:
        _fail(label, str(exc))
        return 1


def _locked_edge(provider, cache_dir) -> int:
    """Holes stay empty, and a fill only replaces those holes.

    The Austin lidar footprint crosses lon -117.465. On Nov 10 the
    service returned 0 for that project's pixels, so this check
    does not require the west side to be a real hill. It requires
    the outside to be nodata, and the fill to change only those
    nodata cells.
    """
    label = "locked edge, holes then one fill"
    box = (-117.470, 39.495, -117.460, 39.500)
    try:
        catalog = provider.list_collections(box)
        partial = [
            item for item in catalog.collections
            if item.kind == "project" and 10 <= item.coverage_pct <= 90
        ]
        national = [
            item for item in catalog.collections
            if item.kind == "national" and item.covers_all and item.resolution_m < 20
        ]
        if not partial or not national:
            shown = ", ".join(
                f"{item.name} {item.coverage_pct:.0f}%"
                for item in catalog.collections
            )
            _fail(label, f"need a partial project and a full 10 m grid; {shown}")
            return 1
        primary = partial[0]
        fill = national[0]
        request = DemRequest(bbox=box, resolution_m=10)
        holes = get_dem(
            request,
            cache_dir=cache_dir,
            provider=provider,
            timeout=180,
            collection_key=primary.key,
        )
        filled = get_dem(
            request,
            cache_dir=cache_dir,
            provider=provider,
            timeout=180,
            collection_key=primary.key,
            fill_key=fill.key,
        )
        hole_pixels = list(Image.open(holes.path).get_flattened_data())
        fill_pixels = list(Image.open(filled.path).get_flattened_data())
        if len(hole_pixels) != len(fill_pixels):
            _fail(label, "holes and fill are different shapes")
            return 1
        hole_count = sum(1 for value in hole_pixels if _is_hole(value))
        kept = 0
        repaired = 0
        problems = []
        for before, after in zip(hole_pixels, fill_pixels):
            if _is_hole(before):
                if _is_hole(after) or not (500 <= after <= 4000):
                    problems.append(
                        f"a hole stayed {after:.1f}, expected the national DEM"
                    )
                    break
                repaired += 1
            else:
                if abs(after - before) > 0.01:
                    problems.append(
                        f"primary {before:.2f} became {after:.2f}"
                    )
                    break
                kept += 1
        if hole_count < len(hole_pixels) * 0.1:
            problems.append(f"only {hole_count} holes in {len(hole_pixels)} px")
        if kept < len(hole_pixels) * 0.1:
            problems.append(f"only {kept} primary pixels")
        if not filled.fill_used:
            problems.append("fill_used is false")
        if filled.fill_name != fill.name:
            problems.append(f"fill name {filled.fill_name}")
        if primary.name not in (filled.source_description or ""):
            problems.append("description omitted the primary")
        if problems:
            _fail(label, "; ".join(problems))
            return 1
        kept_values = [value for value in hole_pixels if not _is_hole(value)]
        _pass(
            label,
            f"{primary.name} kept {kept} px "
            f"({min(kept_values):.1f}-{max(kept_values):.1f} m), "
            f"{fill.name} filled {repaired} holes",
        )
        return 0
    except Exception as exc:
        _fail(label, str(exc))
        return 1


def _tiled_matches_one_request(provider, johnson, reference: Path, cache_dir) -> int:
    """Several small requests stitch back to the one-request file.

    max_pixels is lowered so the Johnson Creek 1 m grid, a couple
    of hundred pixels on a side, has to be more than one request.
    The real server limit stays 8000. This only forces the split.
    """
    label = "tiled download matches one request"
    try:
        if not reference.is_file():
            _fail(label, f"missing {reference}")
            return 1
        tiled_provider = create_provider(
            "3dep",
            timeout=180,
            verify_ssl=provider.verify_ssl,
            max_pixels=100,
        )
        request = DemRequest(bbox=johnson, resolution_m=1)
        sized = tiled_provider.estimate(request)
        result = get_dem(
            request,
            cache_dir=cache_dir,
            provider=tiled_provider,
            timeout=180,
        )
        if sized.n_requests < 2:
            _fail(label, f"expected several requests, got {sized.n_requests}")
            return 1
        if "Stitched" not in result.source_description:
            _fail(label, result.source_description)
            return 1
        tiled_image, scale = _read_tiff(Path(result.path))
        reference_image, _ref_scale = _read_tiff(reference)
        tiled = list(tiled_image.get_flattened_data())
        original = list(reference_image.get_flattened_data())
        if len(tiled) != len(original):
            _fail(label, f"{len(tiled)} px vs {len(original)} px")
            return 1
        worst = 0.0
        for left, right in zip(original, tiled):
            worst = max(worst, abs(left - right))
        if scale is None or abs(float(scale[0]) - 1.0) > 0.01:
            _fail(label, f"pixel scale {scale}")
            return 1
        if worst > 0.05:
            _fail(label, f"largest difference {worst:.3f} m")
            return 1
        _pass(
            label,
            f"{sized.n_requests} requests, largest difference {worst:.4f} m",
        )
        return 0
    except Exception as exc:
        _fail(label, str(exc))
        return 1


def run() -> int:
    provider = _provider()
    out_dir = Path(tempfile.gettempdir()) / "tributary-dem-smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0

    print("3DEP smoke")
    print("-" * 40)

    # About 220 m on a side. Small enough to be quick, big
    # enough that 1 m and 10 m are different grids.
    johnson = _box(_JC_LON, _JC_LAT, 0.001)
    try:
        resolutions = provider.available_resolutions(johnson)
        finest = min(resolutions)
        coarsest = max(resolutions)
        if finest > 1.5:
            _fail(
                "Johnson Creek catalog",
                f"expected lidar (<= 1.5 m), got {resolutions}",
            )
            failures += 1
        elif coarsest < 10:
            _fail(
                "Johnson Creek catalog",
                f"expected the national DEM in the stack, got {resolutions}",
            )
            failures += 1
        else:
            _pass(
                "Johnson Creek catalog",
                f"finest {finest:g} m, coarsest {coarsest:g} m",
            )
    except Exception as exc:
        _fail("Johnson Creek catalog", str(exc))
        failures += 1
        resolutions = []

    for pixel_m in (1, 10):
        label = f"Johnson Creek {pixel_m:g} m"
        try:
            request = DemRequest(bbox=johnson, resolution_m=pixel_m)
            estimate = provider.estimate(request)
            path = out_dir / f"johnson_{pixel_m:g}m.tif"
            result = provider.fetch(request, str(path))
            image, scale = _read_tiff(path)
            problems = []
            if image.mode != "F":
                problems.append(f"mode {image.mode}, expected F")
            if image.size != (estimate.width_px, estimate.height_px):
                problems.append(
                    f"size {image.size}, estimate "
                    f"{estimate.width_px} x {estimate.height_px}"
                )
            if scale is None or abs(float(scale[0]) - pixel_m) > 0.01:
                problems.append(f"pixel scale {scale}, expected {pixel_m}")
            if result.requested_resolution_m != pixel_m:
                problems.append(
                    f"requested {result.requested_resolution_m}"
                )
            if result.source_resolution_m is None or result.source_resolution_m > 1.5:
                problems.append(
                    f"source {result.source_resolution_m}, expected lidar"
                )
            if result.epsg != 26910:
                problems.append(f"epsg {result.epsg}")
            pixels = list(image.get_flattened_data())
            finite = [value for value in pixels if value == value]
            if not finite:
                problems.append("no finite elevations")
            elif min(finite) < 0 or max(finite) > 400:
                problems.append(
                    f"elevations {min(finite):.1f} to {max(finite):.1f} m"
                )
            if problems:
                _fail(label, "; ".join(problems))
                failures += 1
            else:
                _pass(
                    label,
                    f"{image.size[0]} x {image.size[1]}  "
                    f"{min(finite):.1f}-{max(finite):.1f} m  "
                    f"source {result.source_resolution_m:g} m  "
                    f"{path}",
                )
        except Exception as exc:
            _fail(label, str(exc))
            failures += 1

    austin = _box(_AUSTIN_LON, _AUSTIN_LAT, 0.0004)
    label = "Austin 1 m request over a 10 m source"
    try:
        request = DemRequest(bbox=austin, resolution_m=1)
        result = provider.fetch(request, str(out_dir / "austin_1m.tif"))
        source = result.source_resolution_m
        if source is None or source < 8:
            _fail(label, f"source resolution {source}, expected ~10 m")
            failures += 1
        elif result.requested_resolution_m != 1:
            _fail(label, f"requested {result.requested_resolution_m}")
            failures += 1
        else:
            _pass(
                label,
                f"asked for 1 m, source is {source:g} m "
                f"(coarsest {result.coarsest_resolution_m:g} m)",
            )
    except Exception as exc:
        _fail(label, str(exc))
        failures += 1

    label = "Johnson Creek collections"
    try:
        catalog = provider.list_collections(johnson)
        suggested = next(
            (
                item for item in catalog.collections
                if item.key == catalog.suggested_key
            ),
            None,
        )
        nationals = [
            item for item in catalog.collections
            if item.kind == "national" and item.covers_all
        ]
        summary = ", ".join(
            f"{item.name} {item.period_label()} {item.coverage_pct:.0f}%"
            for item in catalog.collections
        )
        if suggested is None or suggested.kind != "project":
            _fail(label, f"suggested {catalog.suggested_key}; {summary}")
            failures += 1
        elif suggested.resolution_m > 2 or not suggested.covers_all:
            _fail(label, f"{suggested.name} {suggested.coverage_pct}%")
            failures += 1
        elif not nationals:
            _fail(label, f"no full-coverage national grid; {summary}")
            failures += 1
        else:
            _pass(label, f"suggest {suggested.name} ({suggested.period_label()})")
    except Exception as exc:
        _fail(label, str(exc))
        failures += 1

    label = "Austin edge partial lidar"
    try:
        # The 2020 lidar footprint ends inside this box. National
        # tiles cover the whole thing. Checked Nov 10, 2026.
        edge = (-117.55, 39.47, -117.40, 39.52)
        catalog = provider.list_collections(edge)
        projects = [item for item in catalog.collections if item.kind == "project"]
        nationals = [
            item for item in catalog.collections
            if item.kind == "national" and item.covers_all
        ]
        partial = [item for item in projects if not item.covers_all]
        if not partial:
            shown = ", ".join(
                f"{item.name} {item.coverage_pct:.0f}%"
                for item in catalog.collections
            )
            _fail(label, f"no partial project; {shown}")
            failures += 1
        elif not nationals:
            _fail(label, "national grid did not cover the box")
            failures += 1
        elif not (15 <= partial[0].coverage_pct <= 85):
            _fail(label, f"{partial[0].name} {partial[0].coverage_pct}%")
            failures += 1
        else:
            _pass(
                label,
                f"{partial[0].name} {partial[0].coverage_pct:.0f}% "
                f"({partial[0].period_label()})",
            )
    except Exception as exc:
        _fail(label, str(exc))
        failures += 1

    locked_dir = out_dir / "locked"
    failures += _locked_johnson(provider, johnson, locked_dir)
    failures += _locked_edge(provider, locked_dir)
    failures += _tiled_matches_one_request(
        provider, johnson, out_dir / "johnson_1m.tif", out_dir / "tiles"
    )

    cache_dir = out_dir / "cache"
    if cache_dir.is_dir():
        for leftover in cache_dir.glob("*"):
            if leftover.is_file():
                leftover.unlink()
    label = "get_dem cache hit"
    try:
        request = DemRequest(bbox=johnson, resolution_m=10)
        first = get_dem(
            request, cache_dir=cache_dir, provider=provider, timeout=120
        )
        second = get_dem(
            request, cache_dir=cache_dir, provider=provider, timeout=120
        )
        if first.from_cache:
            _fail(label, "first call was already cached")
            failures += 1
        elif not second.from_cache:
            _fail(label, "second call missed the cache")
            failures += 1
        elif first.path != second.path:
            _fail(label, f"{first.path} vs {second.path}")
            failures += 1
        else:
            _pass(label, second.path)
    except Exception as exc:
        _fail(label, str(exc))
        failures += 1

    label = "get_dem over the 250 MB cap"
    try:
        # ~7.9 km. Under 8000 px, over 250 MB, so the cap is what
        # refuses it. No TIFF should be written.
        lat, lon = 45.37, -122.59
        wide = (lon, lat, lon + 7900 / (111320 * 0.7023), lat + 7850 / 111320)
        grid = pixel_grid(wide, 1)
        if grid.width_px > 8000 or grid.height_px > 8000:
            _fail(label, f"box is past the pixel limit: {grid}")
            failures += 1
        else:
            request = DemRequest(bbox=wide, resolution_m=1)
            before = list(cache_dir.glob("*.tif"))
            try:
                get_dem(
                    request,
                    max_mb=DEFAULT_MAX_MB,
                    cache_dir=cache_dir,
                    provider=provider,
                    timeout=120,
                )
            except ValueError as exc:
                message = str(exc)
                after = list(cache_dir.glob("*.tif"))
                if "MB" not in message or "250" not in message:
                    _fail(label, message)
                    failures += 1
                elif len(after) != len(before):
                    _fail(label, "a TIFF was written for an over-cap box")
                    failures += 1
                else:
                    _pass(
                        label,
                        f"refused {grid.width_px} x {grid.height_px} px, "
                        "no TIFF written",
                    )
            else:
                _fail(label, "download was not refused")
                failures += 1
    except Exception as exc:
        _fail(label, str(exc))
        failures += 1

    print("-" * 40)
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("All smoke checks passed")
    print(f"GeoTIFFs: {out_dir}")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
