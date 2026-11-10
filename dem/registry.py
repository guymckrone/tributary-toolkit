"""Catalog of DEM providers.

Same idea as data_sources/registry.py: callers ask for "3dep"
instead of constructing ThreeDepProvider themselves. A second
elevation source later is one entry here plus one class.

Factories stay lazy. Importing this module does not open a
session until create_provider() is called, and even that does
not hit the network.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List

from .providers.base import DemProvider
from .providers.threedep import MAX_PIXELS, ThreeDepProvider


@dataclass(frozen=True)
class ProviderDescriptor:
    """One row in the DEM catalog."""

    key: str
    display_name: str
    description: str
    factory: Callable[..., DemProvider]


_PROVIDERS: tuple = (
    ProviderDescriptor(
        key="3dep",
        display_name="USGS 3DEP",
        description=(
            "USGS 3D Elevation Program bare-earth DEM. "
            "National coverage. 1 m lidar where it has been "
            "published, otherwise the 10 m or 30 m national DEM, "
            "resampled to whatever pixel size you request."
        ),
        factory=lambda **kwargs: ThreeDepProvider(
            timeout=kwargs.get("timeout", 90),
            verify_ssl=kwargs.get("verify_ssl", True),
            max_pixels=kwargs.get("max_pixels", MAX_PIXELS),
        ),
    ),
)

_BY_KEY: Dict[str, ProviderDescriptor] = {d.key: d for d in _PROVIDERS}


def list_providers() -> List[ProviderDescriptor]:
    """Every registered provider, in picker order."""
    return list(_PROVIDERS)


def get_descriptor(key: str) -> ProviderDescriptor:
    try:
        return _BY_KEY[key]
    except KeyError:
        known = ", ".join(d.key for d in _PROVIDERS) or "(none)"
        raise ValueError(
            f"Unknown DEM provider {key!r}. Known providers: {known}"
        ) from None


def create_provider(key: str, **kwargs) -> DemProvider:
    """A fresh provider. Network starts on the first method call."""
    return get_descriptor(key).factory(**kwargs)
