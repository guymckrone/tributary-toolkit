"""Shapes the terrain tools share.

Same habit as dem.models: frozen dataclasses, validated on the
way in, so a bad request fails before Whitebox or USGS is touched.

TerrainAoi is where the DEM comes from. PrepParams is how the
drainage gets built from it. PrepResult is what came out and
what I used to make it.
"""

from dataclasses import dataclass
from typing import Optional, Tuple, Union

from dem.models import BBox, DemRequest
from dem.service import DEFAULT_MAX_MB


@dataclass(frozen=True)
class TerrainAoi:
    """Where the DEM comes from. Either a box to download or a file.

    bbox: (west, south, east, north) lon/lat, same as DemRequest.
        Goes through get_dem, so collection lock, fill, clip, and
        the DEM cache all behave the way the DEM page does.
    dem_path: a GeoTIFF already on disk. Nothing is downloaded.
        I refuse bbox and dem_path together. I would have to pick
        one, and that is a guess.
    clip: a Boundary from dem.read_boundary(), or GeoJSON. Only
        used with bbox.
    """

    bbox: Optional[BBox] = None
    resolution_m: Union[float, str] = "best"
    provider: str = "3dep"
    collection_key: Optional[str] = None
    fill_key: Optional[str] = None
    clip: Optional[object] = None
    verify_ssl: bool = True
    max_mb: float = DEFAULT_MAX_MB
    dem_path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.bbox is None and not self.dem_path:
            raise ValueError("Give a bbox to download, or a dem_path on disk.")
        if self.bbox is not None and self.dem_path:
            raise ValueError(
                "Give a bbox or a dem_path, not both. "
                "I won't guess which one you meant."
            )
        if self.dem_path and (
            self.collection_key or self.fill_key or self.clip is not None
        ):
            raise ValueError(
                "collection_key, fill_key, and clip only apply to a "
                "download. A dem_path is used as it is."
            )
        if self.max_mb <= 0:
            raise ValueError(f"max_mb must be > 0, got {self.max_mb}")
        if self.bbox is not None:
            # DemRequest checks resolution and provider. Build one now
            # so a bad value fails here, not halfway through a run.
            self.dem_request()

    def dem_request(self) -> DemRequest:
        if self.bbox is None:
            raise ValueError("A dem_path AOI has no DemRequest.")
        return DemRequest(
            bbox=tuple(float(value) for value in self.bbox),
            resolution_m=self.resolution_m,
            provider=self.provider,
        )


@dataclass(frozen=True)
class PrepParams:
    """How breach, D8, and streams are run.

    The stream threshold is required, in one of two forms:

    accum_threshold: cells of upstream area. Absolute.
    accum_fraction: share of this DEM's own accumulation maximum,
        0 to 1. Slice 0 showed why: on a 600 m box the maximum
        was 55,465 cells, so the plan's 50,000 drew 2 stream cells
        and 500,000 drew none. 0.10 gave a fuller network and 0.30
        a single thread on that 1 m box.

    There is no default on purpose. No single number works for
    every box size and resolution.

    breach_dist: max_dist for breach_depressions_least_cost, in
        cells. Slice 0 used 50. The library default is 100.
    mainstem_only: kept for the plan's contract. Not built yet.
        I refuse True rather than hand back the full network
        labeled as a mainstem.
    """

    accum_threshold: Optional[float] = None
    accum_fraction: Optional[float] = None
    breach_dist: int = 50
    fill_deps: bool = False
    mainstem_only: bool = False

    def __post_init__(self) -> None:
        if (self.accum_threshold is None) == (self.accum_fraction is None):
            raise ValueError(
                "Give exactly one of accum_threshold (cells) or "
                "accum_fraction (share of the accumulation max)."
            )
        if self.accum_threshold is not None:
            threshold = float(self.accum_threshold)
            if not threshold > 0:
                raise ValueError(
                    f"accum_threshold must be > 0 cells, got {self.accum_threshold}"
                )
            object.__setattr__(self, "accum_threshold", threshold)
        if self.accum_fraction is not None:
            fraction = float(self.accum_fraction)
            if not 0 < fraction <= 1:
                raise ValueError(
                    f"accum_fraction must be between 0 and 1, got {self.accum_fraction}"
                )
            object.__setattr__(self, "accum_fraction", fraction)
        if isinstance(self.breach_dist, bool) or int(self.breach_dist) != self.breach_dist:
            raise ValueError(f"breach_dist must be whole cells, got {self.breach_dist}")
        if self.breach_dist <= 0:
            raise ValueError(f"breach_dist must be > 0 cells, got {self.breach_dist}")
        object.__setattr__(self, "breach_dist", int(self.breach_dist))
        if self.mainstem_only:
            raise ValueError(
                "mainstem_only isn't built yet. Prep returns every "
                "extracted stream. Leave it False."
            )


@dataclass(frozen=True)
class PrepResult:
    """The four prep rasters plus what I used to make them.

    Paths are Float32 GeoTIFFs in the terrain cache. Cells with no
    DEM are WHITEBOX_NODATA (-32768) in all four, not NaN. Streams
    are 1 on the channel and 0 off it.

    accum_threshold is the number of cells actually used. When the
    caller gave accum_fraction, that is fraction x accum_max.
    """

    cache_key: str
    dem_path: str
    dem_identity: str
    epsg: int
    cell_size_m: float
    rows: int
    columns: int
    breached_path: str
    pointer_path: str
    accum_path: str
    streams_path: str
    accum_max: float
    accum_threshold: float
    accum_fraction: Optional[float]
    stream_cells: int
    breach_dist: int
    fill_deps: bool
    nodata: float
    whitebox_version: str
    # Copied from DemResult when the DEM came from get_dem. None
    # for a dem_path, where I only know the file.
    dem_source: Optional[str] = None
    dem_collection_name: Optional[str] = None
    dem_source_resolution_m: Optional[float] = None
    from_cache: bool = False

    @property
    def paths(self) -> Tuple[str, str, str, str]:
        return (
            self.breached_path,
            self.pointer_path,
            self.accum_path,
            self.streams_path,
        )
