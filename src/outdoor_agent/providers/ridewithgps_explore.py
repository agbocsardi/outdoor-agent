"""EXPERIMENTAL, undocumented Ride with GPS public Explore discovery (no credentials).

This adapter reads ``https://ridewithgps.com/explore.json``, the website endpoint
that backs the public Explore map. It is **not** the documented Ride with GPS API,
it is **not** a licensed interface, and its permission, stability and pagination
semantics are unverified. Direct-origin evidence (2026-10-10, project HTTPClient,
no credentials, cookies or VPN) showed HTTP 200 ``application/json`` and a
``robots.txt`` that does not disallow ``/explore.json``; that is evidence, not a
legal determination.

Only bounded, read-only, unauthenticated GETs are performed. Results carry a
simplified polyline only, so they are never treated as navigation-complete
geometry. No ``ride`` CLI, credentials or account access are involved.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.geometry import distance_m
from outdoor_agent.geo.gpx import validate_source_url
from outdoor_agent.models.routes import Activity, Metric, Route, SourceType
from outdoor_agent.providers.base import SearchCriteria, SearchResult
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.osm import activity_matches
from outdoor_agent.providers.registry import Provider, registry

EXPLORE_URL = "https://ridewithgps.com/explore.json"
_EXPLORE_PATH = "/explore.json"
_EXPLORE_HOSTS = frozenset({"ridewithgps.com", "www.ridewithgps.com"})
_ROUTE_URL = re.compile(r"^/routes/([1-9][0-9]{0,18})$")

# Safety bounds; all are hard stops, never silent completion.
_MAX_PAGES = 20
_MAX_RECORDS = 1000
_MAX_BYTES = 8 * 1024 * 1024
_TOTAL_BUDGET_SECONDS = 30.0

_TRUNCATION_WARNING = (
    "Ride with GPS Explore pagination stopped at a safety bound; additional matches "
    "may be omitted and this result is not a complete catalog."
)
_EXPERIMENTAL_WARNING = (
    "Ride with GPS public discovery uses an undocumented website endpoint, not the "
    "documented API; permission, stability and pagination semantics are unverified."
)

# Plural selector names verified against the live endpoint; guessed singulars
# (activity_type, track_type, surface, distance_min, distance_max) are ignored.
_ACTIVITY_SELECTOR = {
    Activity.road: "cycling:road",
    Activity.gravel: "cycling:gravel",
    Activity.mtb: "cycling:mountain",
    Activity.hiking: "walking:hiking",
    Activity.walking: "walking:generic",
    Activity.running: "running:generic",
    Activity.trail_running: "running:generic",
}
_ACTIVITY = {
    "cycling:road": Activity.road,
    "cycling:gravel": Activity.gravel,
    "cycling:mountain": Activity.mtb,
    "walking:hiking": Activity.hiking,
    "walking:generic": Activity.walking,
    "running:generic": Activity.running,
}
# Query parameters a same-origin next-page pointer may carry. Anything else is
# rejected loudly rather than followed.
_ALLOWED_NEXT_PARAMS = frozenset(
    {
        "models",
        "bounding_box",
        "activity_types",
        "track_types",
        "surfaces",
        "length_min",
        "length_max",
        "page",
        "page_size",
        "offset",
        "per_page",
        "limit",
    }
)
_RAW_METADATA_KEYS = (
    "unpaved_pct",
    "surface",
    "track_type",
    "difficulty",
    "terrain",
    "locality",
    "country_code",
    "likes_count",
    "reviews_count",
)
_GEOMETRY_WARNING = (
    "Explore returns a simplified polyline only; full navigation geometry requires "
    "the permitted Ride with GPS route detail endpoint."
)


def _meters(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _explore_params(criteria: SearchCriteria) -> dict[str, str | int | float]:
    south, west, north, east = criteria.bbox
    params: dict[str, str | int | float] = {
        "models": "routes",
        "bounding_box": f"{south},{west},{north},{east}",
    }
    selector = _ACTIVITY_SELECTOR.get(criteria.activity)
    if selector is not None:
        params["activity_types"] = selector
    if criteria.distance_min_m is not None:
        params["length_min"] = _meters(criteria.distance_min_m)
    if criteria.distance_max_m is not None:
        params["length_max"] = _meters(criteria.distance_max_m)
    if criteria.shape == "loop" or criteria.loop == "required":
        params["track_types"] = "loop"
    elif criteria.shape == "point_to_point":
        params["track_types"] = "out_and_back"
    return params


def _explore_page(content: bytes) -> tuple[list[dict[str, Any]], str | None]:
    try:
        payload = json.loads(content)
        results = payload["results"]
        total = payload["total_count"]
        if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
            raise ValueError("Invalid results")
        if isinstance(total, bool) or not isinstance(total, int):
            raise ValueError("Invalid total count")
        next_page: str | None = None
        meta = payload.get("meta")
        if meta is not None:
            if not isinstance(meta, dict):
                raise ValueError("Invalid meta")
            explore = meta.get("explore")
            if explore is not None:
                if not isinstance(explore, dict):
                    raise ValueError("Invalid explore meta")
                candidate = explore.get("next_page_url")
                if candidate is not None and not isinstance(candidate, str):
                    raise ValueError("Invalid next page pointer")
                next_page = candidate
        return list(results), next_page
    except (KeyError, TypeError, ValueError) as exc:
        raise OutdoorError(
            "PROVIDER_SCHEMA_INVALID",
            "Ride with GPS Explore returned an invalid public route list.",
            4,
            provider="ridewithgps_public",
        ) from exc


def _validated_next_url(value: str) -> str:
    """Accept only a same-origin Explore pointer with allow-listed query parameters."""
    if not isinstance(value, str) or len(value) > 2048:
        raise _schema_error("Pagination pointer is not a bounded string.")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _schema_error("Pagination pointer is malformed.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _EXPLORE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
        or parsed.path != _EXPLORE_PATH
        or not parsed.query
    ):
        raise _schema_error("Pagination pointer is not a same-origin Explore URL.")
    params = parse_qs(parsed.query, keep_blank_values=True)
    if set(params) - _ALLOWED_NEXT_PARAMS:
        raise _schema_error("Pagination pointer carries disallowed query parameters.")
    return value


def _schema_error(message: str) -> OutdoorError:
    return OutdoorError(
        "PROVIDER_SCHEMA_INVALID",
        f"Ride with GPS Explore pagination was rejected: {message}",
        4,
        provider="ridewithgps_public",
    )


def _page_key(url: str, params: dict[str, str | int | float] | None) -> str:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in (params or {}).items():
        query.setdefault(key, []).append(str(value))
    normalized = sorted((key, tuple(sorted(values))) for key, values in query.items())
    return repr((parsed.scheme, parsed.hostname, parsed.path, normalized))


def _activity_compatible(requested: Activity, actual: Activity) -> bool:
    if requested in {Activity.walking, Activity.hiking} and actual in {
        Activity.walking,
        Activity.hiking,
    }:
        return True
    return activity_matches(requested, actual)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    return None


def _metric(value: Any, source_url: str, observed_at: datetime) -> Metric | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return Metric(value=float(value), source_url=source_url, observed_at=observed_at)


def _route_from_item(item: dict[str, Any], fetched_at: datetime) -> Route:
    try:
        route_id = item["id"]
        if isinstance(route_id, bool) or not isinstance(route_id, int) or route_id <= 0:
            raise ValueError("Invalid route id")
        if item.get("type") != "route":
            raise ValueError("Unexpected result type")
        match = _ROUTE_URL.fullmatch(item["url"]) if isinstance(item.get("url"), str) else None
        if match is None or match.group(1) != str(route_id):
            raise ValueError("Invalid route URL")
        source_url = validate_source_url(f"https://ridewithgps.com{item['url']}")
        if source_url is None:
            raise ValueError("Missing source URL")
    except (KeyError, TypeError, ValueError, OutdoorError) as exc:
        raise OutdoorError(
            "PROVIDER_SCHEMA_INVALID",
            "Ride with GPS Explore returned an invalid public route record.",
            4,
            provider="ridewithgps_public",
        ) from exc
    activity_types = item.get("activity_types")
    activity = (
        next(
            (_ACTIVITY[entry] for entry in activity_types if entry in _ACTIVITY),
            Activity.other,
        )
        if isinstance(activity_types, list)
        else Activity.other
    )
    track_type = item.get("track_type")
    is_loop = None if track_type is None else track_type == "loop"
    byline = item.get("byline_name")
    attribution = (
        f"Ride with GPS community route by {byline}"
        if isinstance(byline, str) and byline.strip()
        else "Ride with GPS community route; author attribution unavailable"
    )
    raw_metadata: dict[str, str | int | float | bool | None] = {}
    for key in _RAW_METADATA_KEYS:
        if key in item:
            raw_metadata[key] = _scalar(item[key])
    value = str(route_id)
    try:
        # The canonical provider stays "ridewithgps" so the normalized id, the
        # authenticated detail/export factories and deduplication all resolve;
        # "ridewithgps_public" is only the discovery-source declaration name.
        return Route(
            id=f"ridewithgps:{value}",
            provider="ridewithgps",
            provider_route_id=value,
            source_url=source_url,
            source_type=SourceType.community,
            attribution=attribution,
            name=str(item.get("name") or f"Ride with GPS route {value}")[:240],
            description=(str(item["description"])[:1000] if item.get("description") else None),
            activity=activity,
            geometry=None,
            start_lat=item.get("first_lat"),
            start_lon=item.get("first_lng"),
            distance_m=item.get("distance"),
            elevation_gain_m=item.get("elevation_gain"),
            is_loop=is_loop,
            reviews_count=_metric(item.get("reviews_count"), source_url, fetched_at),
            popularity=_metric(item.get("likes_count"), source_url, fetched_at),
            created_at=_timestamp(item.get("created_at")),
            updated_at=_timestamp(item.get("updated_at")),
            fetched_at=fetched_at,
            geometry_quality="unknown",
            gpx_access="unverified",
            warnings=[_GEOMETRY_WARNING],
            raw_metadata=raw_metadata,
        )
    except ValidationError as exc:
        raise OutdoorError(
            "PROVIDER_SCHEMA_INVALID",
            "Ride with GPS Explore returned an invalid public route record.",
            4,
            provider="ridewithgps_public",
        ) from exc


def _passes_filters(route: Route, criteria: SearchCriteria) -> bool:
    if not _activity_compatible(criteria.activity, route.activity):
        return False
    if route.distance_m is None:
        if criteria.distance_min_m is not None or criteria.distance_max_m is not None:
            return False
    elif (criteria.distance_min_m is not None and route.distance_m < criteria.distance_min_m) or (
        criteria.distance_max_m is not None and route.distance_m > criteria.distance_max_m
    ):
        return False
    if (criteria.loop == "required" or criteria.shape == "loop") and route.is_loop is not True:
        return False
    if criteria.shape == "point_to_point" and route.is_loop is not False:
        return False
    if criteria.center is not None and criteria.radius_km is not None:
        if route.start_lon is None or route.start_lat is None:
            return False
        start = (route.start_lon, route.start_lat)
        if distance_m(criteria.center, start) > criteria.radius_km * 1000:
            return False
    return True


class RideWithGPSExplore:
    """Read-only client for the undocumented, unauthenticated Explore endpoint."""

    def __init__(self, settings: Settings, http: HTTPClient) -> None:
        self.settings = settings
        self.http = http
        self.declaration: Provider = next(
            item for item in registry() if item.provider == "ridewithgps_public"
        )

    async def search(self, criteria: SearchCriteria) -> SearchResult:
        params = _explore_params(criteria)
        routes: list[Route] = []
        warnings: list[str] = [_EXPERIMENTAL_WARNING]
        truncated = False
        cached = False
        stale = False
        raw_records = 0
        pages = 0
        seen: set[str] = set()
        next_url: str | None = None
        try:
            async with asyncio.timeout(_TOTAL_BUDGET_SECONDS):
                while True:
                    if pages >= _MAX_PAGES:
                        truncated = next_url is not None
                        break
                    fetch_params: dict[str, str | int | float] | None
                    if next_url is None:
                        url, fetch_params = EXPLORE_URL, params
                    else:
                        url, fetch_params = next_url, None
                    key = _page_key(url, fetch_params)
                    if key in seen:
                        warnings.append(_TRUNCATION_WARNING)
                        truncated = True
                        break
                    seen.add(key)
                    result = await self.http.fetch(
                        "GET",
                        url,
                        provider="ridewithgps_public",
                        params=fetch_params,
                        ttl=self.settings.discovery_cache_ttl_seconds,
                        persist=True,
                        attempts=2,
                        max_bytes=_MAX_BYTES,
                    )
                    pages += 1
                    if pages == 1:
                        cached = result.from_cache
                    stale = stale or result.stale
                    items, next_page = _explore_page(result.content)
                    raw_records += len(items)
                    for item in items:
                        route = _route_from_item(item, result.fetched_at)
                        if _passes_filters(route, criteria):
                            routes.append(route)
                            if len(routes) >= criteria.limit:
                                break
                    if len(routes) >= criteria.limit:
                        break
                    if next_page is None:
                        break
                    if raw_records >= _MAX_RECORDS:
                        truncated = True
                        break
                    next_url = _validated_next_url(next_page)
        except TimeoutError:
            if not routes and pages == 0:
                raise OutdoorError(
                    "PROVIDER_TIMEOUT",
                    "Ride with GPS Explore did not respond within the wall-time budget.",
                    4,
                    provider="ridewithgps_public",
                    retryable=True,
                ) from None
            warnings.append(_TRUNCATION_WARNING)
            truncated = True
        if truncated and _TRUNCATION_WARNING not in warnings:
            warnings.append(_TRUNCATION_WARNING)
        return SearchResult(routes=routes, warnings=warnings, cached=cached, stale=stale)


class RideWithGPSExploreAdapter:
    """Discovery protocol adapter for the experimental public Explore catalog."""

    def __init__(self, provider: RideWithGPSExplore) -> None:
        self.provider = provider
        self.declaration: Provider = provider.declaration

    async def search(self, criteria: SearchCriteria) -> SearchResult:
        return await self.provider.search(criteria)
