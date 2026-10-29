"""Live smoke checks for StreamStats (SS-Delineate).

Run from the data-downloader folder:

    python -m streamstats.smoke

Hits real USGS endpoints — separate from tests/hammer_test.py
so I don't burn the Water Data quota. Oregon only for now.
"""

from __future__ import annotations

import sys

from .client import StreamStatsClient

# Johnson Creek area — on-network in earlier live checks.
_OR_GOOD_LAT = 45.3725
_OR_GOOD_LON = -122.5858

# Off-network-ish — returns a tiny junk polygon we reject.
_OR_BAD_LAT = 45.4791
_OR_BAD_LON = -122.6475

_REGION = "OR"


def _pass(label: str, detail: str = "") -> None:
    # ASCII hyphen so Windows consoles don't garble an em dash.
    suffix = f" - {detail}" if detail else ""
    print(f"PASS  {label}{suffix}")


def _fail(label: str, detail: str) -> None:
    print(f"FAIL  {label} - {detail}")


def run() -> int:
    """Run OR snap + watershed checks. Return 0 on success."""
    client = StreamStatsClient(timeout=90)
    failures = 0

    print(f"StreamStats smoke ({_REGION})")
    print("-" * 40)

    # --- 1) snap on a good point ---
    try:
        snapped = client.snap(
            _OR_GOOD_LAT, _OR_GOOD_LON, region=_REGION
        )
        if not snapped.get("snappable"):
            _fail(
                "snap (on-network)",
                f"snappable=False warning={snapped.get('warning')!r}",
            )
            failures += 1
        else:
            _pass(
                "snap (on-network)",
                f"{snapped['latitude']:.6f}, {snapped['longitude']:.6f}",
            )
    except Exception as exc:
        _fail("snap (on-network)", str(exc))
        failures += 1

    # --- 2) watershed on the same good point ---
    try:
        basin = client.watershed(
            _OR_GOOD_LAT, _OR_GOOD_LON, region=_REGION
        )
        verts = basin.get("vertex_count") or 0
        if verts < StreamStatsClient.MIN_BASIN_VERTICES:
            _fail(
                "watershed (on-network)",
                f"too few vertices ({verts})",
            )
            failures += 1
        elif not (basin.get("basin_geojson") or {}).get("geometry"):
            _fail("watershed (on-network)", "missing geometry")
            failures += 1
        else:
            _pass(
                "watershed (on-network)",
                f"{basin.get('geometry_type')} "
                f"vertices={verts} area={basin.get('shape_area')}",
            )
    except Exception as exc:
        _fail("watershed (on-network)", str(exc))
        failures += 1

    # --- 3) off-network should raise (degenerate basin) ---
    try:
        client.watershed(
            _OR_BAD_LAT, _OR_BAD_LON, region=_REGION
        )
        _fail(
            "watershed (off-network)",
            "expected ValueError for degenerate basin",
        )
        failures += 1
    except ValueError as exc:
        msg = str(exc).lower()
        if "degenerate" in msg or "empty" in msg:
            _pass("watershed (off-network)", "raised as expected")
        else:
            _fail(
                "watershed (off-network)",
                f"ValueError but unexpected text: {exc}",
            )
            failures += 1
    except Exception as exc:
        _fail("watershed (off-network)", str(exc))
        failures += 1

    print("-" * 40)
    if failures:
        print(f"{failures} check(s) failed")
        return 1

    print("All smoke checks passed")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
