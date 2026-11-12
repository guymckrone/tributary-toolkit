"""Shapes every DEM caller shares.

A DEM is a raster file, not a time series, so these are not
DataPoints and this package is not a BaseDataSource. Same idea
as streamstats/: a sibling with its own types.

requested pixel size and source resolution are different fields
on purpose. The 3DEP server will resample a 10 m DEM onto 1 m
pixels and not say so. Callers (the download page, later REM)
need both numbers.
"""

from dataclasses import dataclass
from typing import Optional, Tuple, Union


BBox = Tuple[float, float, float, float]


@dataclass(frozen=True)
class DemRequest:
    """What a caller asks for. No network, no file yet.

    bbox: (west, south, east, north) in lon/lat. Same order as
        the site-search API.
    resolution_m: pixel size in meters, or "best" (finest the
        provider has here). "best" is resolved by the provider,
        not here.
    provider: registry key. "3dep" is the only one for now.
    out_crs: "utm" means the UTM zone of the box center
        (NAD83, EPSG 269xx). A specific EPSG code can come later.
    """

    bbox: BBox
    resolution_m: Union[float, str] = "best"
    provider: str = "3dep"
    out_crs: str = "utm"

    def __post_init__(self) -> None:
        if self.resolution_m != "best":
            try:
                resolution = float(self.resolution_m)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "resolution_m must be a positive number of meters "
                    f"or 'best', got {self.resolution_m!r}"
                ) from exc
            if resolution <= 0:
                raise ValueError(
                    f"resolution_m must be > 0, got {resolution}"
                )
            object.__setattr__(self, "resolution_m", resolution)
        if self.out_crs != "utm":
            raise ValueError(
                f"out_crs {self.out_crs!r} isn't supported yet. "
                "Use 'utm'."
            )


@dataclass(frozen=True)
class DemEstimate:
    """Size of a download before anyone fetches it.

    estimated_mb is uncompressed Float32 (4 bytes per pixel),
    decimal MB. The GeoTIFF runs a bit larger because it is
    tiled. n_requests stays 1 until tiling exists.
    """

    provider: str
    resolution_m: float
    width_px: int
    height_px: int
    estimated_mb: float
    n_requests: int = 1
    over_cap: bool = False
    # Finest and coarsest source cells the catalog reported.
    # None means we haven't asked the catalog. They can differ
    # inside one box (lidar next to the national 10 m DEM).
    source_resolution_m: Optional[float] = None
    coarsest_resolution_m: Optional[float] = None


@dataclass(frozen=True)
class DemResult:
    """A finished GeoTIFF plus what a caller needs to trust it."""

    path: str
    provider: str
    requested_resolution_m: float
    # Native cell size of the best source in the AOI. May be
    # coarser than requested_resolution_m. None if unknown.
    source_resolution_m: Optional[float]
    # Coarsest source cell that also overlaps the box.
    coarsest_resolution_m: Optional[float]
    epsg: int
    # xmin, ymin, xmax, ymax in that EPSG, after pixel snapping.
    bbox_projected: Tuple[float, float, float, float]
    vertical_unit: str
    source_description: str
    from_cache: bool = False
    # Set when the download was locked to one collection. Empty
    # on the old server-mosaic path, and on cache files written
    # before the lock existed.
    collection_key: Optional[str] = None
    collection_name: Optional[str] = None
    collection_period: Optional[str] = None
    fill_key: Optional[str] = None
    fill_name: Optional[str] = None
    fill_period: Optional[str] = None
    # True only when the fill raster was actually copied into holes.
    # A fill that was named but not downloaded (primary already
    # covers the box) stays False, with the name still filled in.
    fill_used: bool = False
    # Pixel value I write outside the locked footprint. None on
    # the mosaic path, where the server picks the value. Set when
    # an uploaded outline punched the corners out, too.
    nodata: Optional[float] = None
    # File name of an uploaded outline, when the raster was clipped
    # to it. None for a plain rectangle.
    clip_name: Optional[str] = None
    # Set when a locked flight's footprint is a sheet of exact
    # zeros. The raster is unchanged. None when there is nothing
    # to flag, including the unlocked mosaic.
    zero_warning: Optional[str] = None
    # False on a sidecar from before this check. The next read
    # can measure that TIFF. True once the check has run,
    # including when the sentence stayed empty.
    zero_warning_known: bool = False


@dataclass(frozen=True)
class DemCollection:
    """One choice in the picker. Tiles from the same flight are already merged.

    kind "project" is a lidar (or other) collection with its own name.
    kind "national" is the seamless 3DEP grid (about 10 m or 30 m),
    which is a fallback, not a single flight.

    year is the end of the collection window when the catalog has
    one, otherwise the acquisition year. National datasets use that
    as "updated through," not "flown this year." period_label()
    is the text to show.
    """

    key: str
    name: str
    kind: str
    resolution_m: float
    year: Optional[int]
    start_text: Optional[str]
    end_text: Optional[str]
    acquisition_year: Optional[int]
    coverage_pct: float
    covers_all: bool
    vertical_datum: str
    # The service's DEM_Type. Every row checked so far was 1, so
    # this is not a bare-earth yes/no. Kept because it is cheap.
    dem_type: Optional[int]
    tile_count: int
    tile_names: Tuple[str, ...]
    # GeoJSON MultiPolygon in lon/lat, or None if the server sent
    # no rings. The map draws this. It is the real footprint, not
    # the box the user asked about.
    footprint: Optional[dict]

    def period_label(self) -> str:
        start_year = self.start_text[:4] if self.start_text else None
        end_year = self.end_text[:4] if self.end_text else None
        if self.kind == "national":
            if end_year:
                return f"through {end_year}"
            return "national seamless"
        if start_year and end_year and start_year != end_year:
            return f"{start_year}-{end_year}"
        if end_year:
            return end_year
        if self.acquisition_year:
            return str(self.acquisition_year)
        return "year unknown"


@dataclass(frozen=True)
class DemCatalog:
    """What overlaps a box, plus which choice I would preselect.

    coverage_spacing_m is how far apart the sample points were.
    covers_all means every sample fell inside the footprint
    (treated as full at 99.5% so one boundary point cannot flip it).
    """

    collections: Tuple[DemCollection, ...]
    suggested_key: Optional[str]
    coverage_spacing_m: float

