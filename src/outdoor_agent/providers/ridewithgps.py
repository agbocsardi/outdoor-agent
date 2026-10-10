"""Documented, authenticated Ride with GPS reads and permitted GPX retrieval only."""

import hashlib
import json
import re
import tempfile
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.geometry import distance_m, within_bbox
from outdoor_agent.geo.gpx import MAX_BYTES, MAX_POINTS, inspect_gpx, validate_source_url
from outdoor_agent.models.routes import Activity, Geometry, GPXArtifact, Route, SourceType
from outdoor_agent.providers.base import SearchCriteria, SearchResult
from outdoor_agent.providers.http import FetchResult, HTTPClient
from outdoor_agent.providers.osm import activity_matches
from outdoor_agent.providers.registry import Provider, registry
from outdoor_agent.providers.ride_cli import RideCLI

_ACTIVITY = {
    "cycling:road": Activity.road,
    "cycling:gravel": Activity.gravel,
    "cycling:mountain": Activity.mtb,
    "walking:hiking": Activity.hiking,
    "walking:generic": Activity.walking,
    "running:generic": Activity.running,
}
_ROUTE_URL = re.compile(r"^/routes/([1-9][0-9]{0,18})$")
_ROUTE_LIST_PATH = "/api/v1/routes.json"
_PAGE_SIZE_MAX = 200
_PAGE_SIZE_MIN = 20
_MAX_SAVED_PAGES = 20
_MAX_SAVED_RECORDS = 500
_SAVED_TRUNCATION_WARNING = (
    "Ride with GPS saved-route discovery stopped at its page or record bound; "
    "additional saved routes may be omitted."
)


def canonical_route_reference(value: str) -> str:
    """Map an exact public Ride with GPS route URL to its provider reference.

    Only ``https://ridewithgps.com/routes/<digits>`` (optionally with the ``www.``
    host) is recognized; query strings, fragments, credentials, ports and other
    paths are returned unchanged.
    """
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"ridewithgps.com", "www.ridewithgps.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            return value
    except ValueError:
        return value
    match = _ROUTE_URL.fullmatch(parsed.path)
    return f"ridewithgps:{match.group(1)}" if match else value


def numeric_route_id(route_id: str, provider: str = "ridewithgps") -> str:
    value = canonical_route_reference(route_id).removeprefix(f"{provider}:")
    if provider == "osm":
        value = value.removeprefix("relation:")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise OutdoorError("INVALID_ROUTE_ID", f"{provider} route IDs must be positive integers.")
    return value


def _next_page_number(value: Any) -> int | None:
    """Extract only the page number from a pagination link; never a fetchable URL."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid pagination link")
    parsed = urlsplit(value)
    if parsed.hostname not in {None, "ridewithgps.com", "www.ridewithgps.com"}:
        raise ValueError("Pagination link host is not Ride with GPS")
    if parsed.path != _ROUTE_LIST_PATH:
        raise ValueError("Pagination link is not the route list endpoint")
    pages = parse_qs(parsed.query).get("page")
    if not pages or not pages[0].isdigit():
        raise ValueError("Pagination link has no page number")
    number = int(pages[0])
    if number < 1:
        raise ValueError("Pagination link page number is out of range")
    return number


def _route_list_page(content: bytes) -> tuple[list[dict[str, Any]], int | None]:
    try:
        payload = json.loads(content)
        routes = payload["routes"]
        pagination = payload["meta"]["pagination"]
        if not isinstance(routes, list) or not isinstance(pagination, dict):
            raise ValueError("Invalid route list")
        if "next_page_url" not in pagination:
            raise ValueError("Missing pagination state")
        if not all(isinstance(item, dict) for item in routes):
            raise ValueError("Invalid route item")
        return list(routes), _next_page_number(pagination["next_page_url"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OutdoorError(
            "PROVIDER_SCHEMA_INVALID",
            "Ride with GPS returned an invalid saved-route list.",
            4,
            provider="ridewithgps",
        ) from exc


def _activity_compatible(requested: Activity, actual: Activity) -> bool:
    if requested in {Activity.walking, Activity.hiking} and actual in {
        Activity.walking,
        Activity.hiking,
    }:
        return True
    return activity_matches(requested, actual)


class RideWithGPS:
    """A configured key/token is required; working personal reads do not imply public search."""

    def __init__(self, settings: Settings, http: HTTPClient) -> None:
        self.settings = settings
        self.http = http
        self.cli = RideCLI(settings)
        self.declaration: Provider = next(
            item for item in registry() if item.provider == "ridewithgps"
        )

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

    def _has_credentials(self) -> bool:
        return bool(self.settings.ride_api_key and self.settings.ride_auth_token)

    async def _fetch_authenticated(
        self, path: str, *, max_bytes: int, query: dict[str, str | int | float] | None = None
    ) -> FetchResult:
        if self._has_credentials():
            try:
                return await self.http.fetch(
                    "GET",
                    f"https://ridewithgps.com{path}",
                    provider="ridewithgps",
                    params=query,
                    headers=self._headers(),
                    persist=False,
                    ttl=0,
                    max_bytes=max_bytes,
                )
            except OutdoorError as exc:
                if exc.code == "AUTH_REQUIRED":
                    raise OutdoorError(
                        "AUTH_EXPIRED",
                        "Configured Ride with GPS credentials were rejected; refresh the credentials securely.",
                        3,
                        provider="ridewithgps",
                    ) from exc
                raise
        if self.cli.executable() is None:
            raise OutdoorError(
                "AUTH_REQUIRED",
                "Ride with GPS needs an authenticated ride CLI or configured credentials.",
                3,
                provider="ridewithgps",
                suggestions=[
                    "Run `ride login` to authenticate the installed ride CLI.",
                    "Or configure RIDEWITHGPS_API_KEY and RIDEWITHGPS_AUTH_TOKEN securely.",
                ],
            )
        return await self.cli.fetch(
            path,
            query={key: str(value) for key, value in query.items()} if query else None,
            max_bytes=max_bytes,
        )

    def _route_from_item(self, item: dict[str, Any], fetched_at: datetime) -> Route:
        """Parse one documented route record; validation, attribution and warnings are shared."""
        try:
            value = numeric_route_id(str(item["id"]))
        except OutdoorError as exc:
            raise ValueError("Invalid provider route id") from exc
        points = item.get("track_points", [])
        if not isinstance(points, list) or len(points) > MAX_POINTS:
            raise ValueError("Invalid geometry")
        coordinates = [(point["x"], point["y"]) for point in points]
        geometry = (
            Geometry(type="LineString", coordinates=coordinates) if len(coordinates) >= 2 else None
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
            fetched_at=fetched_at,
            geometry_quality="complete" if geometry else "unknown",
            gpx_access="unverified",
            warnings=warnings,
        )

    async def get(self, route_id: str) -> Route:
        value = numeric_route_id(route_id)
        result = await self._fetch_authenticated(
            f"/api/v1/routes/{value}.json",
            max_bytes=16 * 1024 * 1024,
        )
        try:
            raw = json.loads(result.content)
            item: dict[str, Any] = raw["route"]
            if not isinstance(item, dict) or str(item.get("id")) != value:
                raise ValueError("Mismatched route")
            return self._route_from_item(item, result.fetched_at)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise OutdoorError(
                "PROVIDER_SCHEMA_INVALID",
                "Ride with GPS returned invalid or changed route metadata.",
                4,
                provider="ridewithgps",
            ) from exc

    async def list_saved_routes(
        self,
        *,
        limit: int,
        activity: Activity | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> list[Route]:
        """List the authenticated user's own saved routes, bounded and paginated.

        The documented endpoint has no activity filter and is scoped to the account;
        this is not a public catalog. Results carry a truncation warning when a page
        or record bound stops pagination before the account is exhausted.
        """
        page_size = min(_PAGE_SIZE_MAX, max(_PAGE_SIZE_MIN, limit))
        page: int | None = 1
        pages = 0
        records: list[Route] = []
        truncated = False
        while page is not None:
            if pages >= _MAX_SAVED_PAGES:
                truncated = page is not None
                break
            if len(_matching(records, activity, bbox)) >= limit:
                break
            result = await self._fetch_authenticated(
                _ROUTE_LIST_PATH,
                max_bytes=8 * 1024 * 1024,
                query={"page": str(page), "page_size": str(page_size)},
            )
            pages += 1
            items, next_page = _route_list_page(result.content)
            if not items:
                break
            try:
                records.extend(self._route_from_item(item, result.fetched_at) for item in items)
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise OutdoorError(
                    "PROVIDER_SCHEMA_INVALID",
                    "Ride with GPS returned an invalid saved-route record.",
                    4,
                    provider="ridewithgps",
                ) from exc
            if len(records) >= _MAX_SAVED_RECORDS:
                records = records[:_MAX_SAVED_RECORDS]
                truncated = next_page is not None
                break
            page = next_page
        if not records:
            raise OutdoorError(
                "RIDEWITHGPS_NO_SAVED_ROUTES",
                "The authenticated Ride with GPS account has no saved routes to discover. Nothing was fabricated and no empty result is returned as success.",
                5,
                provider="ridewithgps",
                suggestions=[
                    "Save routes in the authenticated Ride with GPS account, then retry.",
                    "Or fetch one explicit public route URL with `get --route-id https://ridewithgps.com/routes/<id>`.",
                    "Or import an entitled local GPX with `import-gpx` and search --provider local.",
                ],
            )
        matching = _matching(records, activity, bbox)
        if truncated:
            for route in matching:
                route.warnings.append(_SAVED_TRUNCATION_WARNING)
        return matching[:limit]

    async def export_bytes(self, route_id: str) -> bytes:
        value = numeric_route_id(route_id)
        result = await self._fetch_authenticated(
            f"/api/v1/routes/{value}.gpx",
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

    async def export_gpx(self, route_id: str) -> GPXArtifact:
        """Return validated original provider bytes in a private temporary artifact.

        The caller owns cleanup of ``path`` after consuming the artifact.
        """
        return _temporary_artifact(
            f"ridewithgps:{numeric_route_id(route_id)}", await self.export_bytes(route_id)
        )

    async def probe_auth(self) -> dict[str, str]:
        result = await self._fetch_authenticated(
            "/api/v1/users/current",
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
            "transport": "http" if self._has_credentials() else "ride_cli",
        }


class RideWithGPSDiscoveryAdapter:
    """Discovery over the authenticated account's own saved routes, not a public catalog."""

    def __init__(self, provider: RideWithGPS) -> None:
        self.provider = provider
        self.declaration: Provider = provider.declaration

    async def search(self, criteria: SearchCriteria) -> SearchResult:
        routes = await self.provider.list_saved_routes(
            limit=criteria.limit, activity=criteria.activity, bbox=criteria.bbox
        )
        return SearchResult(routes=routes, warnings=[])


def _matching(
    routes: list[Route],
    activity: Activity | None,
    bbox: tuple[float, float, float, float] | None,
) -> list[Route]:
    return [
        route
        for route in routes
        if (activity is None or _activity_compatible(activity, route.activity))
        and (bbox is None or within_bbox(route, bbox))
    ]


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else None


def _temporary_artifact(route_id: str, content: bytes) -> GPXArtifact:
    try:
        with tempfile.NamedTemporaryFile(
            prefix="outdoor-agent-", suffix=".gpx", delete=False
        ) as file:
            file.write(content)
            file.flush()
            return GPXArtifact(
                route_id=route_id,
                path=file.name,
                sha256=hashlib.sha256(content).hexdigest(),
                byte_count=len(content),
            )
    except OSError as exc:
        raise OutdoorError(
            "EXPORT_WRITE_FAILED", "Cannot stage provider GPX artifact locally.", 7
        ) from exc
