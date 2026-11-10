"""What every DEM provider has to implement.

One subclass plus one registry entry is the whole cost of a
second source later. Nothing here talks to a server.
"""

from abc import ABC, abstractmethod
from typing import List

from ..models import BBox, DemEstimate, DemRequest, DemResult


class DemProvider(ABC):
    """Fetch a GeoTIFF for a lon/lat box.

    available_resolutions reports native source cell sizes
    (meters), not the pixel size we might ask the server to
    resample to. Finest first.
    """

    @property
    @abstractmethod
    def key(self) -> str:
        """Registry id, stable and lowercase. Example: '3dep'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """What a human sees in the picker."""

    @abstractmethod
    def available_resolutions(self, bbox: BBox) -> List[float]:
        """Native cell sizes in meters that cover this box.

        Finest first. Empty means the provider has nothing here.
        """

    @abstractmethod
    def estimate(self, request: DemRequest) -> DemEstimate:
        """Size the download. Do not fetch the raster."""

    @abstractmethod
    def fetch(self, request: DemRequest, out_path: str) -> DemResult:
        """Write a GeoTIFF to out_path and describe it."""

    def list_collections(self, bbox: BBox):
        """Picker rows. Not every provider has a catalog yet."""
        raise ValueError(
            f"{self.display_name} has no collection catalog."
        )

    def locked_estimate(self, request, resolution_m, primary, fill=None):
        """Size a download that is locked to one collection."""
        raise ValueError(
            f"{self.display_name} cannot lock a collection yet."
        )

    def fetch_locked(
        self,
        request: DemRequest,
        out_path: str,
        primary,
        fill,
        resolution_m: float,
    ) -> DemResult:
        """Write a GeoTIFF for one collection, holes punched out.

        fill is a second collection used only outside primary, or
        None when the caller wants holes left empty.
        """
        raise ValueError(
            f"{self.display_name} cannot lock a collection yet."
        )
