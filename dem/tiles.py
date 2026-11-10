"""Split one snapped grid into requests the ImageServer will accept.

The server refuses a side longer than 8000 pixels. The pieces are
slices of the same pixel grid, so they share the collection, the
pixel size, and the edges. A one-pixel halo is requested around
each piece and thrown away. Bilinear interpolation at a raw edge
uses neighbors the next piece does not have. The halo gives every
kept pixel the same neighbors a single download would have used.

A grid that already fits is one piece with no halo, which is the
old single request.
"""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class TileWindow:
    """Core pixels to keep, and the request rectangle around them.

    Columns and rows are indexes into the full grid. Row 0 is north.
    The halo bounds (col0_h and the rest) are clipped to the grid,
    so a piece on the outer edge has no halo on that side.
    """

    col0: int
    row0: int
    col1: int
    row1: int
    col0_h: int
    row0_h: int
    col1_h: int
    row1_h: int

    @property
    def core_width(self) -> int:
        return self.col1 - self.col0

    @property
    def core_height(self) -> int:
        return self.row1 - self.row0

    @property
    def request_width(self) -> int:
        return self.col1_h - self.col0_h

    @property
    def request_height(self) -> int:
        return self.row1_h - self.row0_h


def tile_windows(
    width: int,
    height: int,
    max_pixels: int,
    halo: int = 1,
) -> Tuple[TileWindow, ...]:
    """Pieces whose requests are at most max_pixels on a side.

    Cores meet at their edges and do not overlap. Together they
    cover every pixel of the full grid.
    """
    if width < 1 or height < 1:
        raise ValueError(f"grid must be at least 1 px, got {width} x {height}")
    if max_pixels < 1:
        raise ValueError(f"max_pixels must be >= 1, got {max_pixels}")
    if width <= max_pixels and height <= max_pixels:
        return (TileWindow(0, 0, width, height, 0, 0, width, height),)
    if halo < 0:
        raise ValueError(f"halo must be >= 0, got {halo}")
    # Leave room for a halo on both sides of every request.
    max_core = max_pixels - 2 * halo
    if max_core < 1:
        raise ValueError(
            f"max_pixels {max_pixels} is too small for a {halo} px halo."
        )
    columns = _segments(width, max_core)
    rows = _segments(height, max_core)
    windows = []
    for row0, row1 in rows:
        for col0, col1 in columns:
            windows.append(TileWindow(
                col0=col0,
                row0=row0,
                col1=col1,
                row1=row1,
                col0_h=max(0, col0 - halo),
                row0_h=max(0, row0 - halo),
                col1_h=min(width, col1 + halo),
                row1_h=min(height, row1 + halo),
            ))
    return tuple(windows)


def request_bounds(xmin, ymax, resolution_m, window: TileWindow):
    """UTM extent and pixel size of the request, including its halo.

    xmin / ymax are the outer edges of the full snapped grid.
    Returns (xmin, ymin, xmax, ymax, width_px, height_px).
    """
    req_xmin = xmin + window.col0_h * resolution_m
    req_xmax = xmin + window.col1_h * resolution_m
    req_ymax = ymax - window.row0_h * resolution_m
    req_ymin = ymax - window.row1_h * resolution_m
    return (
        req_xmin,
        req_ymin,
        req_xmax,
        req_ymax,
        window.request_width,
        window.request_height,
    )


def crop_to_core(values, window: TileWindow):
    """Drop the halo. The result is the core, ready to paste."""
    row0 = window.row0 - window.row0_h
    col0 = window.col0 - window.col0_h
    core = values[
        row0:row0 + window.core_height,
        col0:col0 + window.core_width,
    ]
    if core.shape != (window.core_height, window.core_width):
        raise ValueError(
            f"Tile came back {values.shape[1]} x {values.shape[0]} px, "
            f"the core would be {window.core_width} x {window.core_height}."
        )
    return core


def _segments(length: int, max_core: int):
    segments = []
    start = 0
    while start < length:
        stop = min(length, start + max_core)
        segments.append((start, stop))
        start = stop
    return segments
