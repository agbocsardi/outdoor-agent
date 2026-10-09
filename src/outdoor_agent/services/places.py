"""Opt-in, bounded Google Places enrichment around sampled GPX route points."""

import json
import math
import os
from typing import TYPE_CHECKING, Any

from pyproj import Geod

from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Coordinate, Geometry, Model, Route
from outdoor_agent.providers.google_places import (
    API_KEY_HEADER,
    CATEGORY_TYPES,
    ENDPOINT,
    FIELD_MASK,
    FIELD_MASK_HEADER,
    MAX_RESULTS,
    RADIUS_METERS,
)

if TYPE_CHECKING:
    from outdoor_agent.config import Settings
    from outdoor_agent.providers.http import HTTPClient

_GEOD = Geod(ellps="WGS84")


class NearbyPlace(Model):
    id: str
    name: str
    formatted_address: str | None = None
    latitude: float
    longitude: float
    google_maps_uri: str | None = None
    types: list[str]
    source: str = "Google Places"


class PlacesResult(Model):
    places: list[NearbyPlace]
    request_count: int
    limits: dict[str, int | float | str]
    source: str = "Google Places API (New)"
    attribution: str = "Google Maps"
    warnings: list[str]


def _route_lines(geometry: Geometry) -> list[list[Coordinate]]:
    if geometry.type == "LineString":
        return [geometry.coordinates]  # type: ignore[list-item]
    return geometry.coordinates  # type: ignore[return-value]


def _sample_route(geometry: Geometry, count: int) -> list[Coordinate]:
    """Choose evenly spaced points by WGS84 distance, preserving disconnected segments."""
    lines = _route_lines(geometry)
    segments: list[tuple[Coordinate, Coordinate, float, float]] = []
    total = 0.0
    for line in lines:
        for start, end in zip(line, line[1:], strict=False):
            length = float(_GEOD.inv(start[0], start[1], end[0], end[1])[2])
            if length > 0:
                segments.append((start, end, length, total))
                total += length
    if total <= 0:
        raise OutdoorError(
            "ROUTE_GEOMETRY_REQUIRED",
            "Route geometry must contain measurable segments for place enrichment.",
            2,
            provider="google",
        )

    samples: list[Coordinate] = []
    for index in range(count):
        target = total * (index + 0.5) / count
        for start, end, length, offset in segments:
            if offset <= target <= offset + length:
                azimuth, _, _ = _GEOD.inv(start[0], start[1], end[0], end[1])
                lon, lat, _ = _GEOD.fwd(start[0], start[1], azimuth, target - offset)
                samples.append((lon, lat))
                break
    # Very short routes can place multiple evenly spaced samples at effectively the same
    # coordinate; one search there is sufficient and prevents duplicate provider calls.
    unique: list[Coordinate] = []
    for point in samples:
        if not any(_GEOD.inv(point[0], point[1], other[0], other[1])[2] < 1 for other in unique):
            unique.append(point)
    return unique


def _parse_places(content: bytes) -> list[NearbyPlace]:
    try:
        payload: Any = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid response")
    raw_places = payload.get("places", [])
    if not isinstance(raw_places, list):
        raise ValueError("invalid response")
    places: list[NearbyPlace] = []
    for item in raw_places:
        if not isinstance(item, dict):
            continue
        place_id = item.get("id")
        display_name = item.get("displayName")
        location = item.get("location")
        if not isinstance(place_id, str) or not isinstance(location, dict):
            continue
        name = display_name.get("text") if isinstance(display_name, dict) else None
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if (
            not isinstance(name, str)
            or not isinstance(latitude, (int, float))
            or not isinstance(longitude, (int, float))
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            continue
        raw_types = item.get("types", [])
        types = (
            [value for value in raw_types if isinstance(value, str)]
            if isinstance(raw_types, list)
            else []
        )
        uri = item.get("googleMapsUri")
        address = item.get("formattedAddress")
        places.append(
            NearbyPlace(
                id=place_id,
                name=name,
                formatted_address=address if isinstance(address, str) else None,
                latitude=latitude,
                longitude=longitude,
                google_maps_uri=uri if isinstance(uri, str) else None,
                types=types,
            )
        )
    return places


async def enrich_places(
    settings: "Settings",
    route: Route,
    category: str,
    provider: str = "google",
    *,
    http_client: "HTTPClient | None" = None,
) -> PlacesResult:
    """Find deduplicated Google Places within 500 m of bounded route samples.

    Each sampled point consumes one request from the per-run budget. Google responses are
    never cached or persisted because storage rights and retention terms are not configured.
    The route itself is not sent: only up to `max_google_requests_per_run` point centers are.
    """
    if provider != "google":
        raise OutdoorError(
            "PROVIDER_UNSUPPORTED", "Place enrichment supports the Google provider only.", 2
        )
    if not settings.google_enabled or "google" in settings.disabled_providers:
        raise OutdoorError(
            "GOOGLE_DISABLED",
            "Google Places enrichment is disabled; enable it explicitly in configuration.",
            5,
            provider="google",
        )
    if category not in CATEGORY_TYPES:
        raise OutdoorError(
            "GOOGLE_CATEGORY_UNSUPPORTED",
            "Unsupported place category. Choose viewpoint, attraction, trailhead, park, campground, or picnic.",
            2,
            provider="google",
        )
    if settings.max_google_requests_per_run < 1:
        raise OutdoorError(
            "REQUEST_BUDGET_EXHAUSTED",
            "Google Places request budget is zero for this run.",
            4,
            provider="google",
        )
    common_limits: dict[str, int | float | str] = {
        "request_budget": settings.max_google_requests_per_run,
        "sample_cap": 20,
        "radius_m": RADIUS_METERS,
        "max_results_per_request": MAX_RESULTS,
        "request_count_semantics": "Nearby Search HTTP sends; retries are disabled",
    }
    if settings.offline:
        return PlacesResult(
            places=[],
            request_count=0,
            limits=common_limits,
            warnings=["Partial result: Google Places enrichment was skipped in offline mode."],
        )
    if route.geometry is None:
        raise OutdoorError(
            "ROUTE_GEOMETRY_REQUIRED",
            "Route geometry is required for near-route place enrichment.",
            2,
            provider="google",
        )
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise OutdoorError(
            "GOOGLE_API_KEY_MISSING",
            "Set GOOGLE_MAPS_API_KEY to enable Google Places enrichment.",
            3,
            provider="google",
        )
    sample_count = min(settings.max_google_requests_per_run, 20)
    centers = _sample_route(route.geometry, sample_count)
    if not centers:
        raise OutdoorError(
            "ROUTE_GEOMETRY_REQUIRED",
            "Route geometry has no measurable segments for place enrichment.",
            2,
            provider="google",
        )

    warnings: list[str] = []
    if category == "viewpoint":
        warnings.append(
            "Google Places has no stable viewpoint type; tourist_attraction is used as a proxy."
        )
    if category == "trailhead":
        warnings.append(
            "Google Places hiking_area is a broad proxy and does not identify trailheads specifically."
        )
    if settings.max_google_requests_per_run > 20:
        warnings.append("The adapter sample cap of 20 route points limited this run.")
    if len(centers) < sample_count:
        warnings.append(
            "Request budget was not fully used because duplicate route samples were removed."
        )

    own_client = http_client is None
    if own_client:
        from outdoor_agent.providers.http import HTTPClient

        http_client = HTTPClient(settings)
        await http_client.__aenter__()
    assert http_client is not None
    unique_places: dict[str, NearbyPlace] = {}
    request_count = 0
    try:
        for longitude, latitude in centers:
            if request_count >= settings.max_google_requests_per_run:
                warnings.append(
                    "Google Places request budget exhausted; remaining route samples were skipped."
                )
                break
            request_count += 1
            body = {
                "includedTypes": [CATEGORY_TYPES[category]],
                "maxResultCount": MAX_RESULTS,
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": latitude, "longitude": longitude},
                        "radius": RADIUS_METERS,
                    }
                },
            }
            try:
                fetched = await http_client.fetch(
                    "POST",
                    ENDPOINT,
                    provider="google",
                    json_body=body,
                    headers={API_KEY_HEADER: api_key, FIELD_MASK_HEADER: FIELD_MASK},
                    max_bytes=512_000,
                    attempts=1,
                    ttl=0,
                    persist=False,
                    min_interval=1.0,
                )
                parsed = _parse_places(fetched.content)
            except OutdoorError as error:
                warnings.append(
                    f"Google Places request {request_count} failed ({error.code}); partial results are shown."
                )
                continue
            except ValueError:
                warnings.append(
                    f"Google Places request {request_count} returned an unusable response; partial results are shown."
                )
                continue
            for place in parsed:
                if (
                    _GEOD.inv(longitude, latitude, place.longitude, place.latitude)[2]
                    <= RADIUS_METERS
                ):
                    unique_places.setdefault(place.id, place)
    finally:
        if own_client:
            await http_client.__aexit__(None, None, None)

    return PlacesResult(
        places=list(unique_places.values()),
        request_count=request_count,
        limits={
            **common_limits,
        },
        warnings=warnings,
    )
