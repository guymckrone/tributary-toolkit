"""DEM download backend.

Sibling of data_sources/ and streamstats/, not a gage source.
Call get_dem() for a cached GeoTIFF. Call list_collections()
before that if the user should pick a flight year. create_provider()
is the lower level, one server and no cache.
"""

from .boundary import Boundary, read_boundary
from .aoi import (
    bbox_to_utm,
    estimate_mb,
    lonlat_to_utm,
    snap_outward,
    utm_zone,
    validate_bbox,
)
from .cache import clear_cache, default_cache_dir
from .models import (
    DemCatalog,
    DemCollection,
    DemEstimate,
    DemRequest,
    DemResult,
)
from .providers import DemProvider, ThreeDepProvider
from .registry import (
    ProviderDescriptor,
    create_provider,
    get_descriptor,
)
from .service import (
    DEFAULT_MAX_MB,
    estimate,
    get_dem,
    list_collections,
    list_providers,
)

__all__ = [
    "DEFAULT_MAX_MB",
    "Boundary",
    "DemCatalog",
    "DemCollection",
    "DemEstimate",
    "DemProvider",
    "DemRequest",
    "DemResult",
    "ProviderDescriptor",
    "ThreeDepProvider",
    "bbox_to_utm",
    "clear_cache",
    "create_provider",
    "default_cache_dir",
    "estimate",
    "estimate_mb",
    "get_dem",
    "get_descriptor",
    "list_collections",
    "list_providers",
    "read_boundary",
    "lonlat_to_utm",
    "snap_outward",
    "utm_zone",
    "validate_bbox",
]
