"""Opt-in Google Places Nearby Search adapter."""

import json
import math
import os
from typing import TYPE_CHECKING, Any, Final

from pyproj import Geod

from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.places import NearbyPlace, PlacesResult
from outdoor_agent.models.routes import Coordinate, Geometry
from outdoor_agent.providers.base import Place as BasePlace
from outdoor_agent.providers.base import PlaceEnrichmentProvider
from outdoor_agent.providers.registry import Provider, registry

if TYPE_CHECKING:
    from outdoor_agent.config import Settings
    from outdoor_agent.providers.http import HTTPClient

ENDPOINT: Final = "https://places.googleapis.com/v1/places:searchNearby"
FIELD_MASK: Final = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.googleMapsUri,places.types"
)
RADIUS_METERS: Final = 500.0
MAX_RESULTS: Final = 10

# These are semantic inputs supported by the service. `viewpoint` deliberately maps to
# tourist_attraction because Places does not expose a stable viewpoint type.
CATEGORY_TYPES: Final[dict[str, str]] = {
    "viewpoint": "tourist_attraction",
    "attraction": "tourist_attraction",
    "trailhead": "hiking_area",
    "park": "park",
    "campground": "campground",
    "picnic": "picnic_ground",
}

FIELD_MASK_HEADER: Final = "X-Goog-FieldMask"
API_KEY_HEADER: Final = "X-Goog-Api-Key"

_GEOD = Geod(ellps="WGS84")


def _google_declaration() -> Provider:
    for item in registry():
        if item.provider == "google":
            return item
    raise RuntimeError("Google Places provider declaration is unavailable")


def _route_lines(geometry: Geometry) -> list[list[Coordinate]]:
    if geometry.type == "LineString":
        return [geometry.coordinates]  # type: ignore[list-item]
    return geometry.coordinates  # type: ignore[return-value]


def _sample_route(geometry: Geometry, count: int) -> list[Coordinate]:
    """Choose evenly spaced points by WGS84 distance, preserving disconnected segments."""
    segments: list[tuple[Coordinate, Coordinate, float, float]] = []
    total = 0.0
    for line in _route_lines(geometry):
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
                source_url=uri if isinstance(uri, str) else None,
                formatted_address=address if isinstance(address, str) else None,
                latitude=latitude,
                longitude=longitude,
                google_maps_uri=uri if isinstance(uri, str) else None,
                types=types,
            )
        )
    return places


class GooglePlacesProvider(PlaceEnrichmentProvider):
    """Google Nearby Search implementation behind the typed place provider boundary."""

    declaration: Provider
    last_result: PlacesResult | None

    def __init__(self, settings: "Settings", *, http_client: "HTTPClient | None" = None) -> None:
        self.settings = settings
        self.http_client = http_client
        self.declaration = _google_declaration()
        self.last_result = None

    async def near_route(self, geometry: Geometry, kind: str) -> list[BasePlace]:
        if not self.settings.google_enabled or "google" in self.settings.disabled_providers:
            raise OutdoorError(
                "GOOGLE_DISABLED",
                "Google Places enrichment is disabled; enable it explicitly in configuration.",
                5,
                provider="google",
            )
        if kind not in CATEGORY_TYPES:
            raise OutdoorError(
                "GOOGLE_CATEGORY_UNSUPPORTED",
                "Unsupported place category. Choose viewpoint, attraction, trailhead, park, campground, or picnic.",
                2,
                provider="google",
            )
        if self.settings.max_google_requests_per_run < 1:
            raise OutdoorError(
                "REQUEST_BUDGET_EXHAUSTED",
                "Google Places request budget is zero for this run.",
                4,
                provider="google",
            )

        limits: dict[str, int | float | str] = {
            "request_budget": self.settings.max_google_requests_per_run,
            "sample_cap": 20,
            "radius_m": RADIUS_METERS,
            "max_results_per_request": MAX_RESULTS,
            "request_count_semantics": "Nearby Search HTTP sends; retries are disabled",
        }
        if self.settings.offline:
            return self._set_result(
                [],
                0,
                limits,
                ["Partial result: Google Places enrichment was skipped in offline mode."],
            )
        api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        if not api_key:
            raise OutdoorError(
                "GOOGLE_API_KEY_MISSING",
                "Set GOOGLE_MAPS_API_KEY to enable Google Places enrichment.",
                3,
                provider="google",
            )

        sample_count = min(self.settings.max_google_requests_per_run, 20)
        centers = _sample_route(geometry, sample_count)
        if not centers:
            raise OutdoorError(
                "ROUTE_GEOMETRY_REQUIRED",
                "Route geometry has no measurable segments for place enrichment.",
                2,
                provider="google",
            )

        warnings: list[str] = []
        if kind == "viewpoint":
            warnings.append(
                "Google Places has no stable viewpoint type; tourist_attraction is used as a proxy."
            )
        if kind == "trailhead":
            warnings.append(
                "Google Places hiking_area is a broad proxy and does not identify trailheads specifically."
            )
        if self.settings.max_google_requests_per_run > 20:
            warnings.append("The adapter sample cap of 20 route points limited this run.")
        if len(centers) < sample_count:
            warnings.append(
                "Request budget was not fully used because duplicate route samples were removed."
            )

        unique_places: dict[str, NearbyPlace] = {}
        request_count = 0
        client = self.http_client
        if client is None:
            from outdoor_agent.providers.http import HTTPClient

            async with HTTPClient(self.settings) as owned_client:
                places, request_count = await self._query(
                    owned_client, api_key, centers, kind, unique_places, warnings
                )
        else:
            places, request_count = await self._query(
                client, api_key, centers, kind, unique_places, warnings
            )
        return self._set_result(places, request_count, limits, warnings)

    async def _query(
        self,
        http_client: "HTTPClient",
        api_key: str,
        centers: list[Coordinate],
        kind: str,
        unique_places: dict[str, NearbyPlace],
        warnings: list[str],
    ) -> tuple[list[NearbyPlace], int]:
        request_count = 0
        for longitude, latitude in centers:
            if request_count >= self.settings.max_google_requests_per_run:
                warnings.append(
                    "Google Places request budget exhausted; remaining route samples were skipped."
                )
                break
            request_count += 1
            body = {
                "includedTypes": [CATEGORY_TYPES[kind]],
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
        return list(unique_places.values()), request_count

    def _set_result(
        self,
        places: list[NearbyPlace],
        request_count: int,
        limits: dict[str, int | float | str],
        warnings: list[str],
    ) -> list[BasePlace]:
        self.last_result = PlacesResult(
            places=places,
            request_count=request_count,
            limits=limits,
            warnings=warnings,
        )
        return list(places)
