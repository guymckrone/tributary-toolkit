"""StreamStats API client.

I talk to the new SS-Delineate services (legacy StreamStats
Services were shut down Jan 30, 2026):

    https://streamstats.usgs.gov/ss-delineate

Done so far:
    _get_json()   — HTTP plumbing
    snap()        — pour point on the network (sshydro)
    watershed()   — basin polygon from the same sshydro call
    delineate()   — thin alias for watershed()

Still stubbed: basin_attributes.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests


class StreamStatsClient:
    """Thin wrapper around USGS SS-Delineate.

    Constructing this stays cheap (like our data sources):
    don't hit the network until a method is called.
    """

    # Base for relative paths I pass to _get_json.
    BASE_URL = "https://streamstats.usgs.gov/ss-delineate"

    # Off-network pour points sometimes still return a 4–5
    # vertex "basin" (one DEM cell). Treat those as junk.
    MIN_BASIN_VERTICES = 8

    def __init__(self, timeout: int = 60, verify_ssl: bool = True):
        # timeout: delineation can be slow.
        # verify_ssl: work-network interception may need False
        # later, same idea as Aquarius/SNOTEL.
        self.timeout = timeout
        self.verify_ssl = verify_ssl

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, application/geo+json",
            "User-Agent": "TributaryToolkit StreamStatsClient",
        })
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(
                urllib3.exceptions.InsecureRequestWarning
            )

    def _get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """GET a SS-Delineate path (or absolute URL) as JSON.

        path: e.g. "/v1/delineate/sshydro/OR" — joined onto
            BASE_URL. Absolute http(s) URLs are used as-is so I
            can probe without fighting urljoin.
        params: query string (lat, lon, …).
        """
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            # urljoin needs a trailing slash on the base or a
            # leading slash on path can drop the last segment.
            base = self.BASE_URL.rstrip("/") + "/"
            url = urljoin(base, path.lstrip("/"))

        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            # Fail fast on 4xx/5xx instead of parsing an error
            # HTML page as JSON later.
            response.raise_for_status()
        except requests.RequestException as exc:
            raise requests.RequestException(
                f"StreamStats request failed: {exc}"
            ) from exc

        return response.json()

    @staticmethod
    def _normalize_region(region: str) -> str:
        """Uppercase state/region code; refuse blanks."""
        if region is None or not str(region).strip():
            raise ValueError(
                "region is required (e.g. 'OR'). SS-Delineate "
                "is state-partitioned — I won't guess."
            )
        return str(region).strip().upper()

    def _sshydro(
        self,
        latitude: float,
        longitude: float,
        region: str,
    ) -> Dict[str, Any]:
        """Raw sshydro JSON for one pour point.

        Same endpoint feeds snap (Step 2) and watershed (Step 3).
        """
        region = self._normalize_region(region)
        return self._get_json(
            f"/v1/delineate/sshydro/{region}",
            params={"lat": latitude, "lon": longitude},
        )

    @staticmethod
    def _named_features(
        sshydro_payload: Dict[str, Any],
        layer_name: str,
    ) -> List[Dict[str, Any]]:
        """Pull GeoJSON features for one named layer.

        sshydro nests layers under
        bcrequest → wsresp → featurecollection (list of lists
        of {name, feature}). I walk that without assuming how
        many inner lists USGS wraps.
        """
        wsresp = (
            (sshydro_payload.get("bcrequest") or {})
            .get("wsresp")
            or {}
        )
        outer = wsresp.get("featurecollection") or []
        found: List[Dict[str, Any]] = []

        # featurecollection is usually [[layer, layer, …]].
        for group in outer:
            layers = group if isinstance(group, list) else [group]
            for layer in layers:
                if not isinstance(layer, dict):
                    continue
                if layer.get("name") != layer_name:
                    continue
                feature = layer.get("feature") or {}
                feats = feature.get("features") or []
                found.extend(feats)

        return found

    def snap(
        self,
        latitude: float,
        longitude: float,
        region: str = "OR",
    ) -> Dict[str, Any]:
        """Snap a point to the StreamStats stream network.

        Calls sshydro and reads the globalwatershedpoint layer.
        GeoJSON coordinates are [lon, lat] — I flip them back
        so callers always see latitude / longitude keys.

        Returns a plain dict:
            latitude, longitude  — snapped pour point
            region               — normalized state code
            request_latitude / request_longitude — what I sent
            huc_id, warning      — from feature properties
            snappable            — False when WarningMsg says
                                   the point wasn't snappable
        """
        region = self._normalize_region(region)
        payload = self._sshydro(latitude, longitude, region)
        # Refuse to guess if zero / many points — same rule as
        # our data sources when multiple series match.
        feature = self._one_named_feature(
            payload,
            "globalwatershedpoint",
            region=region,
            latitude=latitude,
            longitude=longitude,
        )
        # GeoJSON Point coords are [lon, lat]; the helper flips
        # them into latitude / longitude keys for callers.
        return self._snap_dict_from_feature(
            feature,
            region=region,
            request_latitude=latitude,
            request_longitude=longitude,
        )

    @staticmethod
    def _geometry_is_empty(geometry: Optional[Dict[str, Any]]) -> bool:
        """True when GeoJSON has no usable coordinates."""
        if not geometry:
            return True
        coords = geometry.get("coordinates")
        if coords is None:
            return True
        # Empty polygon: [] or [[]] — both show up from SS-Delineate
        # when the upstream basin layer has nothing.
        if coords == [] or coords == [[]]:
            return True
        return False

    @staticmethod
    def _count_vertices(coordinates: Any) -> int:
        """Count coordinate pairs in nested GeoJSON rings."""
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            return 0
        # A position is [lon, lat, …] — numbers, not nested lists.
        if not isinstance(coordinates[0], (list, tuple)):
            return 1
        return sum(
            StreamStatsClient._count_vertices(part)
            for part in coordinates
        )

    def _snap_dict_from_feature(
        self,
        feature: Dict[str, Any],
        *,
        region: str,
        request_latitude: float,
        request_longitude: float,
    ) -> Dict[str, Any]:
        """Build the same snap dict snap() returns, from one feature."""
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            raise ValueError(
                "globalwatershedpoint has no coordinates "
                f"(region={region}, lat={request_latitude}, "
                f"lon={request_longitude})."
            )

        snapped_lon = float(coords[0])
        snapped_lat = float(coords[1])
        props = feature.get("properties") or {}
        warning = (props.get("WarningMsg") or "").strip(" ,")
        snappable = "not snappable" not in warning.lower()

        return {
            "latitude": snapped_lat,
            "longitude": snapped_lon,
            "region": region,
            "request_latitude": float(request_latitude),
            "request_longitude": float(request_longitude),
            "huc_id": props.get("HUCID") or props.get("HUC_ID") or "",
            "warning": warning,
            "snappable": snappable,
        }

    def _one_named_feature(
        self,
        payload: Dict[str, Any],
        layer_name: str,
        *,
        region: str,
        latitude: float,
        longitude: float,
    ) -> Dict[str, Any]:
        """Exactly one feature for a layer, or raise."""
        features = self._named_features(payload, layer_name)
        if not features:
            raise ValueError(
                f"No {layer_name} in sshydro response "
                f"for region={region}, lat={latitude}, "
                f"lon={longitude}."
            )
        if len(features) > 1:
            raise ValueError(
                f"Expected one {layer_name}, got {len(features)} "
                f"for region={region}, lat={latitude}, "
                f"lon={longitude}."
            )
        return features[0]

    def watershed(
        self,
        latitude: float,
        longitude: float,
        region: str = "OR",
    ) -> Dict[str, Any]:
        """Delineate a watershed for one pour point.

        One sshydro call → snapped point + globalwatershed
        polygon. Rejects empty / missing basin geometry instead
        of handing the UI a blank Feature.

        Returns:
            snap            — same dict shape as snap()
            basin_geojson   — the basin Feature (Polygon)
            geometry_type   — e.g. "Polygon"
            vertex_count    — rough size of the ring(s)
            shape_area      — USGS Shape_Area when present
            warning         — basin WarningMsg (may differ
                              from snap["warning"])
        """
        region = self._normalize_region(region)
        payload = self._sshydro(latitude, longitude, region)

        point_feature = self._one_named_feature(
            payload,
            "globalwatershedpoint",
            region=region,
            latitude=latitude,
            longitude=longitude,
        )
        snap_info = self._snap_dict_from_feature(
            point_feature,
            region=region,
            request_latitude=latitude,
            request_longitude=longitude,
        )

        basin_feature = self._one_named_feature(
            payload,
            "globalwatershed",
            region=region,
            latitude=latitude,
            longitude=longitude,
        )
        geometry = basin_feature.get("geometry") or {}
        if self._geometry_is_empty(geometry):
            raise ValueError(
                "globalwatershed geometry is empty "
                f"(region={region}, lat={latitude}, "
                f"lon={longitude}). Point may be off-network "
                f"or outside StreamStats coverage."
            )

        props = basin_feature.get("properties") or {}
        basin_warning = (props.get("WarningMsg") or "").strip(" ,")
        shape_area = props.get("Shape_Area")
        vertex_count = self._count_vertices(
            geometry.get("coordinates")
        )
        if vertex_count < self.MIN_BASIN_VERTICES:
            raise ValueError(
                "globalwatershed looks degenerate "
                f"({vertex_count} vertices, need >= "
                f"{self.MIN_BASIN_VERTICES}; "
                f"region={region}, lat={latitude}, "
                f"lon={longitude}). "
                f"snap snappable={snap_info['snappable']}; "
                f"warning={basin_warning or snap_info['warning']!r}."
            )

        return {
            "snap": snap_info,
            "basin_geojson": basin_feature,
            "geometry_type": geometry.get("type") or "",
            "vertex_count": vertex_count,
            "shape_area": shape_area,
            "warning": basin_warning,
            "region": region,
        }

    def delineate(
        self,
        latitude: float,
        longitude: float,
        region: str = "OR",
    ) -> Dict[str, Any]:
        """Convenience alias — same as watershed().

        One network call; snap lives under result["snap"].
        """
        return self.watershed(latitude, longitude, region=region)

    def basin_attributes(self, workspace_id: str):
        """Fetch StreamStats characteristics for a workspace.

        Not implemented yet — later chat (SS-Hydro / NSS).
        """
        raise NotImplementedError(
            "StreamStatsClient.basin_attributes() — later chat"
        )
