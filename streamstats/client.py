"""StreamStats API client.

I talk to the new SS-Delineate services (legacy StreamStats
Services were shut down Jan 30, 2026):

    https://streamstats.usgs.gov/ss-delineate

Step 1 (this file so far): cheap construct + _get_json only.
Snap / watershed / basin attributes come in later steps —
stubs below still raise until then.
"""

from typing import Any, Dict, Optional
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

    def snap(self, latitude: float, longitude: float):
        """Snap a point to the StreamStats stream network.

        Not implemented yet — Step 2.
        """
        raise NotImplementedError(
            "StreamStatsClient.snap() — Step 2"
        )

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
