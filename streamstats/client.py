"""StreamStats API client.

I talk to the new SS-Delineate services (legacy StreamStats
Services were shut down Jan 30, 2026):

    https://streamstats.usgs.gov/ss-delineate

Done so far:
    _get_json()  — HTTP plumbing
    snap()       — pour point on the network (sshydro)

Still stubbed: watershed, basin_attributes.
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
        points = self._named_features(payload, "globalwatershedpoint")

        if not points:
            raise ValueError(
                f"No globalwatershedpoint in sshydro response "
                f"for region={region}, lat={latitude}, "
                f"lon={longitude}."
            )
        if len(points) > 1:
            # Refuse to guess which pour point — same rule as
            # our data sources when multiple series match.
            raise ValueError(
                f"Expected one globalwatershedpoint, got "
                f"{len(points)} for region={region}, "
                f"lat={latitude}, lon={longitude}."
            )

        feature = points[0]
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            raise ValueError(
                "globalwatershedpoint has no coordinates "
                f"(region={region}, lat={latitude}, "
                f"lon={longitude})."
            )

        # GeoJSON Point: [longitude, latitude]
        snapped_lon = float(coords[0])
        snapped_lat = float(coords[1])

        props = feature.get("properties") or {}
        warning = (props.get("WarningMsg") or "").strip(" ,")
        # USGS sometimes returns a warning even with a point —
        # surface it so the UI can show "results may be inaccurate".
        snappable = "not snappable" not in warning.lower()

        return {
            "latitude": snapped_lat,
            "longitude": snapped_lon,
            "region": region,
            "request_latitude": float(latitude),
            "request_longitude": float(longitude),
            "huc_id": props.get("HUCID") or props.get("HUC_ID") or "",
            "warning": warning,
            "snappable": snappable,
        }

    def watershed(self, latitude: float, longitude: float):
        """Delineate a watershed for one pour point.

        Not implemented yet — Step 3.
        """
        raise NotImplementedError(
            "StreamStatsClient.watershed() — Step 3"
        )

    def basin_attributes(self, workspace_id: str):
        """Fetch StreamStats characteristics for a workspace.

        Not implemented yet — later chat (SS-Hydro / NSS).
        """
        raise NotImplementedError(
            "StreamStatsClient.basin_attributes() — later chat"
        )
