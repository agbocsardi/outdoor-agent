"""Documented, authenticated Ride with GPS reads and permitted GPX retrieval only."""

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.geometry import distance_m
from outdoor_agent.geo.gpx import MAX_BYTES, MAX_POINTS, inspect_gpx, validate_source_url
from outdoor_agent.models.routes import Activity, Geometry, Route, SourceType
from outdoor_agent.providers.http import HTTPClient

_ACTIVITY = {
    "cycling:road": Activity.road,
    "cycling:gravel": Activity.gravel,
    "cycling:mountain": Activity.mtb,
    "walking:hiking": Activity.hiking,
    "walking:generic": Activity.walking,
    "running:generic": Activity.running,
}


def numeric_route_id(route_id: str, provider: str = "ridewithgps") -> str:
    value = route_id.removeprefix(f"{provider}:")
    if provider == "osm":
        value = value.removeprefix("relation:")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise OutdoorError("INVALID_ROUTE_ID", f"{provider} route IDs must be positive integers.")
    return value


class RideWithGPS:
    """A configured key/token is required; working personal reads do not imply public search."""

    def __init__(self, settings: Settings, http: HTTPClient) -> None:
        self.settings = settings
        self.http = http

    def _headers(self) -> dict[str, str]:
        if not self.settings.ride_api_key or not self.settings.ride_auth_token:
            raise OutdoorError(
                "AUTH_REQUIRED",
                "Configure RIDEWITHGPS_API_KEY and RIDEWITHGPS_AUTH_TOKEN through secure environment injection or private TOML.",
                3,
                provider="ridewithgps",
                suggestions=[
                    "Use a legally obtained local GPX instead; never paste credentials into chat."
                ],
            )
        return {
            "x-rwgps-api-key": self.settings.ride_api_key,
            "x-rwgps-auth-token": self.settings.ride_auth_token,
        }

    async def get(self, route_id: str) -> Route:
        value = numeric_route_id(route_id)
        result = await self.http.fetch(
            "GET",
            f"https://ridewithgps.com/api/v1/routes/{value}.json",
            provider="ridewithgps",
            headers=self._headers(),
            persist=False,
            ttl=0,
            max_bytes=16 * 1024 * 1024,
        )
        try:
            raw = json.loads(result.content)
            item: dict[str, Any] = raw["route"]
            if not isinstance(item, dict) or str(item.get("id")) != value:
                raise ValueError("Mismatched route")
            points = item.get("track_points", [])
            if not isinstance(points, list) or len(points) > MAX_POINTS:
                raise ValueError("Invalid geometry")
            coordinates = [(point["x"], point["y"]) for point in points]
            geometry = (
                Geometry(type="LineString", coordinates=coordinates)
                if len(coordinates) >= 2
                else None
            )
            activity_types = item.get("activity_types", [])
            activity = next(
                (_ACTIVITY[entry] for entry in activity_types if entry in _ACTIVITY), Activity.other
            )
            source_url = validate_source_url(
                item.get("html_url") or f"https://ridewithgps.com/routes/{value}"
            )
            if source_url is None or not source_url.startswith("https://ridewithgps.com/"):
                raise ValueError("Invalid route source")
            creator_id = item.get("user_id")
            attribution = (
                f"Ride with GPS route creator #{creator_id}"
                if isinstance(creator_id, int)
                else "Ride with GPS; creator attribution unavailable"
            )
            warnings = [
                "Authenticated metadata access does not establish public search or GPX export entitlement."
            ]
            if geometry is None:
                warnings.append("Provider returned no complete line geometry.")
            return Route(
                id=f"ridewithgps:{value}",
                provider="ridewithgps",
                provider_route_id=value,
                source_url=source_url,
                source_type=SourceType.community,
                attribution=attribution,
                name=item.get("name") or f"Ride with GPS route {value}",
                description=item.get("description"),
                activity=activity,
                geometry=geometry,
                start_lat=item.get("first_lat"),
                start_lon=item.get("first_lng"),
                end_lat=item.get("last_lat"),
                end_lon=item.get("last_lng"),
                distance_m=item.get("distance"),
                elevation_gain_m=item.get("elevation_gain"),
                is_loop=distance_m(coordinates[0], coordinates[-1]) <= 20 if geometry else None,
                created_at=_timestamp(item.get("created_at")),
                updated_at=_timestamp(item.get("updated_at")),
                fetched_at=result.fetched_at,
                geometry_quality="complete" if geometry else "unknown",
                gpx_access="unverified",
                warnings=warnings,
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise OutdoorError(
                "PROVIDER_SCHEMA_INVALID",
                "Ride with GPS returned invalid or changed route metadata.",
                4,
                provider="ridewithgps",
            ) from exc

    async def export_bytes(self, route_id: str) -> bytes:
        value = numeric_route_id(route_id)
        result = await self.http.fetch(
            "GET",
            f"https://ridewithgps.com/api/v1/routes/{value}.gpx",
            provider="ridewithgps",
            headers=self._headers(),
            persist=False,
            ttl=0,
            max_bytes=MAX_BYTES,
        )
        # Entitlement is enforced by the provider; successful bytes must still be valid GPX.
        try:
            inspect_gpx(result.content)
        except OutdoorError as exc:
            raise OutdoorError(
                "PROVIDER_GPX_INVALID",
                "Provider export is not a valid bounded GPX file.",
                4,
                provider="ridewithgps",
            ) from exc
        return result.content

    async def probe_auth(self) -> dict[str, str]:
        result = await self.http.fetch(
            "GET",
            "https://ridewithgps.com/api/v1/users/current",
            provider="ridewithgps",
            headers=self._headers(),
            persist=False,
            ttl=0,
            max_bytes=1024 * 1024,
        )
        try:
            payload = json.loads(result.content)
            account_id = payload["user"]["id"]
            if not isinstance(account_id, int):
                raise ValueError("Invalid account")
        except (KeyError, TypeError, ValueError) as exc:
            raise OutdoorError(
                "PROVIDER_SCHEMA_INVALID",
                "Provider authentication probe returned invalid metadata.",
                4,
                provider="ridewithgps",
            ) from exc
        return {
            "state": "authenticated",
            "account_reference": hashlib.sha256(str(account_id).encode()).hexdigest()[:12],
        }


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else None
