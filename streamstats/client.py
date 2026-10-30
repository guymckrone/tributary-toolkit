"""StreamStats API client.

I talk to the new SS-Delineate services (legacy StreamStats
Services were shut down Jan 30, 2026):

    https://streamstats.usgs.gov/ss-delineate

Done so far:
    _get_json()   — HTTP plumbing
    snap()        — pour point on the network (sshydro)
    watershed()   — basin polygon from the same sshydro call
    delineate()   — thin alias for watershed()
    basin_characteristics() — SS-Hydro, from that delineation
    flow_statistics()       — NSS regressions, from the polygon
                              plus those characteristics
    basin_attributes()      — alias for basin_characteristics()
"""

from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
import copy


class StreamStatsClient:
    """Thin wrapper around USGS SS-Delineate.

    Constructing this stays cheap (like our data sources):
    don't hit the network until a method is called.
    """

    # Base for relative paths I pass to _get_json / _post_json.
    BASE_URL = "https://streamstats.usgs.gov/ss-delineate"
    HYDRO_URL = "https://streamstats.usgs.gov/ss-hydro"
    # National Streamflow Statistics. Still the regression service
    # after the old StreamStats workspace API was shut down.
    NSS_URL = "https://streamstats.usgs.gov/nssservices"

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
        base: Optional[str] = None,
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
            base_url = (base or self.BASE_URL).rstrip("/") + "/"
            url = urljoin(base_url, path.lstrip("/"))

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

    def _post_json(
        self,
        path: str,
        body: Any,
        base: Optional[str] = None,
    ) -> Any:
        """POST JSON and read JSON back. Same error wrapping as _get_json."""
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            root = (base or self.BASE_URL).rstrip("/") + "/"
            url = urljoin(root, path.lstrip("/"))

        try:
            response = self.session.post(
                url,
                json=body,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
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
            # SS-Hydro calculate wants this object back, plus the
            # state code. A bare wsresp was rejected (422).
            "bcrequest": payload.get("bcrequest"),
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

    def available_characteristics(self, region: str) -> List[Dict[str, Any]]:
        """Codes SS-Hydro can compute for one state. No basin yet."""
        region = self._normalize_region(region)
        payload = self._get_json(
            f"/v1/basin-characteristics/{region}",
            base=self.HYDRO_URL,
        )
        if not isinstance(payload, list):
            raise ValueError(
                "SS-Hydro did not list basin characteristics "
                f"for region={region}."
            )
        return payload

    def basin_characteristics(
        self,
        bcrequest: Dict[str, Any],
        region: str,
        labels: str = "*",
    ) -> List[Dict[str, Any]]:
        """Basin characteristics for a delineation I already have.

        I send the sshydro bcrequest back. SS-Hydro does not
        delineate again. labels is "*" or a semicolon list of codes.
        """
        region = self._normalize_region(region)
        if not isinstance(bcrequest, dict) or "wsresp" not in bcrequest:
            raise ValueError(
                "bcrequest from the delineation is missing "
                "(need wsresp). Delineate again, then ask for "
                "characteristics."
            )
        payload = self._post_json(
            "/v1/basin-characteristics/calculate",
            {
                "bcLabels": labels or "*",
                "stateAbbreviation": region,
                "bcrequest": bcrequest,
            },
            base=self.HYDRO_URL,
        )
        if not isinstance(payload, list) or not payload:
            raise ValueError(
                "SS-Hydro returned no basin characteristics "
                f"for region={region}."
            )
        rows: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            rows.append({
                "code": item.get("code") or "",
                "name": item.get("name") or "",
                "description": item.get("description") or "",
                "unit": item.get("unit") or "",
                "value": item.get("value"),
                "msg": item.get("msg") or "",
            })
        if not rows:
            raise ValueError(
                "SS-Hydro returned no basin characteristics "
                f"for region={region}."
            )
        return rows

    def flow_statistics(
        self,
        basin_feature: Dict[str, Any],
        characteristics: List[Dict[str, Any]],
        region: str,
        statistic_groups: Optional[List[Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Regression flow estimates for one basin.

        USGS workflow (Jan 2026): regression regions for the
        polygon, scenarios for those regions, then fill each
        scenario parameter from the characteristics and estimate.
        I do not invent a missing parameter. A value outside the
        published range is still sent, and the warning is kept.
        """
        region = self._normalize_region(region)
        geometry = self._nss_geometry(basin_feature)
        lookup = {}
        for row in characteristics or []:
            code = str(row.get("code") or "").upper()
            if code:
                lookup[code] = row.get("value")

        groups = self._get_json(
            "/statisticgroups",
            params={"regions": region},
            base=self.NSS_URL,
        )
        if not isinstance(groups, list) or not groups:
            raise ValueError(
                f"NSS returned no statistic groups for region={region}."
            )
        wanted = self._pick_group_ids(groups, statistic_groups)
        group_ids = ",".join(str(item) for item in wanted)

        located = self._post_json(
            "/regressionregions/bylocation",
            geometry,
            base=self.NSS_URL,
        )
        if not isinstance(located, list) or not located:
            raise ValueError(
                "NSS found no regression regions for this basin "
                f"(region={region})."
            )
        region_codes = ",".join(
            str(item.get("code"))
            for item in located
            if item.get("code")
        )
        scenarios = self._get_json(
            "/scenarios",
            params={
                "regions": region,
                "statisticgroups": group_ids,
                "regressionregions": region_codes,
            },
            base=self.NSS_URL,
        )
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError(
                "NSS returned no regression scenarios for this "
                f"basin (region={region})."
            )

        body, skipped = self._fill_scenarios(scenarios, lookup)
        if not body:
            missing = sorted({
                code
                for _group, _name, codes in skipped
                for code in codes
            })
            raise ValueError(
                "Regression scenarios need basin characteristics "
                f"I do not have: {', '.join(missing)}. "
                "I will not guess those values."
            )

        estimated = self._post_json(
            f"/scenarios/estimate?regions={region}",
            body,
            base=self.NSS_URL,
        )
        if not isinstance(estimated, list):
            raise ValueError(
                "NSS estimate did not return a list of scenarios "
                f"(region={region})."
            )
        rows = self._flatten_estimates(estimated)
        for group_name, region_name, codes in skipped:
            rows.append({
                "group": group_name,
                "code": "",
                "name": region_name,
                "value": None,
                "unit": "",
                "regression_region": region_name,
                "errors_or_limits": (
                    "Skipped. Missing characteristics: "
                    + ", ".join(codes)
                ),
                "equation": "",
            })
        if not rows:
            raise ValueError(
                "NSS returned no flow statistics "
                f"for region={region}."
            )
        return rows

    @staticmethod
    def _nss_geometry(basin_feature: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(basin_feature, dict):
            raise ValueError("Basin polygon is missing.")
        geometry = basin_feature.get("geometry") or basin_feature
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError(
                "Flow statistics need the basin polygon "
                "(Polygon or MultiPolygon)."
            )
        return geometry

    @staticmethod
    def _pick_group_ids(
        groups: List[Dict[str, Any]],
        statistic_groups: Optional[List[Any]],
    ) -> List[Any]:
        if not statistic_groups:
            return [item["id"] for item in groups if item.get("id") is not None]
        wanted = []
        known = {
            str(item.get("id")): item.get("id")
            for item in groups
        }
        known.update({
            str(item.get("code") or "").upper(): item.get("id")
            for item in groups
            if item.get("code")
        })
        unknown = []
        for item in statistic_groups:
            key = str(item).upper() if not str(item).isdigit() else str(item)
            if key not in known and str(item) not in known:
                unknown.append(str(item))
                continue
            wanted.append(known.get(str(item), known.get(key)))
        if unknown:
            raise ValueError(
                "Unknown statistic group(s): "
                + ", ".join(unknown)
                + ". I will not guess."
            )
        return wanted

    @staticmethod
    def _fill_scenarios(scenarios, lookup):
        """Copy scenarios and set parameter values. Drop incomplete regions."""
        body = []
        skipped = []
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            kept = []
            group_name = scenario.get("statisticGroupName") or ""
            for region in scenario.get("regressionRegions") or []:
                missing = []
                filled = copy.deepcopy(region)
                for param in filled.get("parameters") or []:
                    code = str(param.get("code") or "").upper()
                    if code not in lookup or lookup[code] is None:
                        missing.append(code or "?")
                        continue
                    param["value"] = lookup[code]
                if missing:
                    skipped.append((
                        group_name,
                        region.get("name") or region.get("code") or "",
                        missing,
                    ))
                    continue
                kept.append(filled)
            if kept:
                copied = copy.deepcopy(scenario)
                copied["regressionRegions"] = kept
                body.append(copied)
        return body, skipped

    @staticmethod
    def _limit_notes(parameters) -> List[str]:
        notes = []
        for param in parameters or []:
            limits = param.get("limits") or {}
            value = param.get("value")
            code = param.get("code") or "parameter"
            low = limits.get("min")
            high = limits.get("max")
            if isinstance(value, (int, float)) and isinstance(low, (int, float)) and value < low:
                notes.append(f"{code} {value} is below the published minimum {low}")
            if isinstance(value, (int, float)) and isinstance(high, (int, float)) and value > high:
                notes.append(f"{code} {value} is above the published maximum {high}")
        return notes

    @classmethod
    def _flatten_estimates(cls, estimated) -> List[Dict[str, Any]]:
        rows = []
        for scenario in estimated:
            if not isinstance(scenario, dict):
                continue
            group_name = scenario.get("statisticGroupName") or ""
            for region in scenario.get("regressionRegions") or []:
                region_name = region.get("name") or region.get("code") or ""
                notes = []
                disclaimer = (region.get("disclaimer") or "").strip()
                if disclaimer:
                    notes.append(disclaimer)
                notes.extend(cls._limit_notes(region.get("parameters")))
                for result in region.get("results") or []:
                    extra = list(notes)
                    for err in result.get("errors") or []:
                        if isinstance(err, dict):
                            extra.append(str(err.get("message") or err))
                        else:
                            extra.append(str(err))
                    unit = result.get("unit") or {}
                    rows.append({
                        "group": group_name,
                        "code": result.get("code") or "",
                        "name": result.get("name") or "",
                        "value": result.get("value"),
                        "unit": unit.get("abbr") or unit.get("unit") or "",
                        "regression_region": region_name,
                        "errors_or_limits": "; ".join(extra),
                        "equation": result.get("equation") or "",
                    })
        return rows

    def basin_attributes(
        self,
        bcrequest: Dict[str, Any],
        region: str,
        labels: str = "*",
    ) -> List[Dict[str, Any]]:
        """Alias for basin_characteristics.

        The old workspace_id argument belonged to the retired
        StreamStats Services API.
        """
        return self.basin_characteristics(
            bcrequest, region, labels=labels
        )
