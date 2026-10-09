"""Read-only discovery and member geometry reconstruction for OSM route relations."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from typing import TYPE_CHECKING, Any, Literal

from pyproj import Geod

from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.export import route_gpx
from outdoor_agent.models.routes import Activity, Geometry, GPXArtifact, Route, SourceType, now
from outdoor_agent.providers.base import SearchCriteria, SearchResult
from outdoor_agent.providers.http import FetchResult, HTTPClient
from outdoor_agent.providers.registry import Provider, registry

if TYPE_CHECKING:
    from outdoor_agent.config import Settings

GEOD = Geod(ellps="WGS84")
OSM_ATTRIBUTION = (
    "© OpenStreetMap contributors (https://www.openstreetmap.org/copyright); "
    "data available under the Open Database License (ODbL)."
)


def overpass_query(
    bbox: tuple[float, float, float, float],
    limit: int = 200,
    activity: Activity | None = None,
) -> str:
    """Build a bounded route-relation query. Bbox order is south,west,north,east."""
    south, west, north, east = bbox
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise OutdoorError("INVALID_BOUNDS", "OSM search bounds must be a non-wrapping WGS84 bbox.")
    if not 1 <= limit <= 500:
        raise OutdoorError("INVALID_LIMIT", "OSM relation limit must be between 1 and 500.")
    bounds = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    activities: dict[Activity, str] = {
        Activity.hiking: "hiking|foot",
        Activity.walking: "hiking|foot",
        Activity.trail_running: "running|hiking|foot",
        Activity.running: "running|hiking|foot",
        Activity.road: "bicycle",
        Activity.gravel: "bicycle",
        Activity.mtb: "mtb",
        Activity.other: "",
    }
    route_filter = (
        activities.get(activity, "hiking|foot|bicycle|mtb|running")
        if activity is not None
        else "hiking|foot|bicycle|mtb|running"
    )
    if not route_filter:
        raise OutdoorError(
            "ACTIVITY_UNSUPPORTED",
            "OSM route relations do not cover the requested activity.",
            5,
            provider="osm",
        )
    return (
        "[out:json][timeout:25];\n"
        f'relation["type"="route"]["route"~"^({route_filter})$"]({bounds});\n'
        f"out body geom {limit};"
    )


class OSMProvider:
    """Overpass relation discovery. Returned routes are mapped trails, not reviews."""

    def __init__(self, settings: Settings, http: HTTPClient | None = None) -> None:
        self.settings = settings
        self.http = http
        self.last_fetch: FetchResult | None = None
        self.warnings: list[str] = []
        self.declaration: Provider = next(item for item in registry() if item.provider == "osm")

    async def search(
        self,
        bbox: tuple[float, float, float, float],
        *,
        activity: Activity | None = None,
        limit: int = 200,
    ) -> list[Route]:
        own_client = self.http is None
        if own_client:
            async with HTTPClient(self.settings) as client:
                return await OSMProvider(self.settings, client).search(
                    bbox, activity=activity, limit=limit
                )
        assert self.http is not None
        response = await self.http.fetch(
            "POST",
            self.settings.overpass_url,
            provider="overpass",
            data={"data": overpass_query(bbox, limit, activity)},
            ttl=self.settings.discovery_cache_ttl_seconds,
            max_bytes=12 * 1024 * 1024,
            min_interval=2.0,
        )
        self.last_fetch = response
        self.warnings = []
        try:
            document = json.loads(response.content)
            elements = document["elements"]
            if not isinstance(elements, list):
                raise ValueError("elements is not a list")
            has_remark = bool(document.get("remark"))
        except (ValueError, TypeError, KeyError) as exc:
            raise OutdoorError(
                "PROVIDER_INVALID_RESPONSE",
                "Overpass returned malformed route data.",
                4,
                provider="osm",
            ) from exc
        if has_remark:
            self.warnings.append("Overpass returned a partial response.")
            if not elements:
                raise OutdoorError(
                    "PROVIDER_PARTIAL_RESPONSE",
                    "Overpass returned no usable routes with a partial-response remark.",
                    4,
                    provider="osm",
                    retryable=True,
                )
        if len(elements) >= limit:
            self.warnings.append(
                f"Overpass returned at least {limit} relations; additional matches may be omitted."
            )
        routes: list[Route] = []
        for element in elements:
            if not isinstance(element, dict) or element.get("type") != "relation":
                continue
            try:
                route = relation_to_route(element, response.fetched_at)
            except (ValueError, TypeError, KeyError, OverflowError):
                self.warnings.append("An OSM route relation was malformed and skipped.")
                continue
            if activity is None or activity_matches(activity, route.activity):
                routes.append(route)
        return routes

    async def get(self, route_id: str) -> Route:
        """Fetch one OSM route relation by numeric ID using a bounded query."""
        value = route_id.removeprefix("osm:").removeprefix("relation:")
        if not value.isdecimal() or len(value) > 19 or int(value) <= 0:
            raise OutdoorError(
                "INVALID_ROUTE_ID",
                "An OSM route ID must be a positive relation number.",
                provider="osm",
            )
        if self.http is None:
            async with HTTPClient(self.settings) as client:
                return await OSMProvider(self.settings, client).get(value)
        self.last_fetch = await self.http.fetch(
            "POST",
            self.settings.overpass_url,
            provider="overpass",
            data={"data": f"[out:json][timeout:25];relation({int(value)});out body geom;"},
            ttl=self.settings.discovery_cache_ttl_seconds,
            max_bytes=4 * 1024 * 1024,
            min_interval=2.0,
        )
        try:
            elements = json.loads(self.last_fetch.content)["elements"]
            element = next(
                item
                for item in elements
                if isinstance(item, dict)
                and item.get("type") == "relation"
                and item.get("id") == int(value)
            )
            tags = element.get("tags", {})
            if not isinstance(tags, dict) or tags.get("type") != "route":
                raise ValueError("Not a route relation")
            route = relation_to_route(element, self.last_fetch.fetched_at)
            if self.last_fetch.stale:
                route.warnings.append("OSM metadata came from a stale offline cache.")
            if json.loads(self.last_fetch.content).get("remark"):
                route.warnings.append("Overpass returned a partial response.")
            return route
        except (ValueError, TypeError, KeyError, StopIteration) as exc:
            raise OutdoorError(
                "ROUTE_NOT_FOUND", "The OSM relation could not be fetched.", 4, provider="osm"
            ) from exc

    async def export_gpx(self, route_id: str) -> GPXArtifact:
        """Encode the current mapped geometry as GPX; this is not original provider GPX."""
        route = await self.get(route_id)
        content = route_gpx(route)
        try:
            with tempfile.NamedTemporaryFile(
                prefix="outdoor-agent-", suffix=".gpx", delete=False
            ) as file:
                file.write(content)
                file.flush()
                return GPXArtifact(
                    route_id=route.id,
                    path=file.name,
                    sha256=hashlib.sha256(content).hexdigest(),
                    byte_count=len(content),
                )
        except OSError as exc:
            raise OutdoorError(
                "EXPORT_WRITE_FAILED", "Cannot stage mapped GPX artifact locally.", 7
            ) from exc


class OSMDiscoveryAdapter:
    """Protocol adapter for the legacy OSM provider search/get interface."""

    def __init__(self, provider: OSMProvider) -> None:
        self.provider = provider
        self.declaration: Provider = next(item for item in registry() if item.provider == "osm")

    async def search(self, criteria: SearchCriteria) -> SearchResult:
        routes = await self.provider.search(
            criteria.bbox, activity=criteria.activity, limit=criteria.limit
        )
        fetch = self.provider.last_fetch
        return SearchResult(
            routes=routes,
            warnings=self.provider.warnings.copy(),
            cached=bool(fetch and fetch.from_cache),
            stale=bool(fetch and fetch.stale),
        )


def relation_to_route(element: dict[str, Any], fetched_at: Any = None) -> Route:
    """Normalize one Overpass relation, preserving unjoined segments as gaps."""
    if element.get("type") != "relation" or not isinstance(element.get("id"), int):
        raise ValueError("Expected OSM relation")
    tags = element.get("tags") or {}
    if not isinstance(tags, dict):
        raise ValueError("Invalid tags")
    route_tag = str(tags.get("route", ""))
    activity = _activity(tags)
    lines, quality = reconstruct_members(element.get("members", []))
    geometry: Geometry | None = None
    if lines:
        geometry = Geometry(
            type="LineString" if len(lines) == 1 else "MultiLineString",
            coordinates=lines[0] if len(lines) == 1 else lines,
        )
    distance = _lines_length(lines) if lines and quality != "incomplete" else None
    start = lines[0][0] if lines else None
    end = lines[-1][-1] if lines else None
    loop = None
    if start and end and quality == "complete":
        _, _, gap = GEOD.inv(start[0], start[1], end[0], end[1])
        loop = gap <= max(100.0, (distance or 0) * 0.02)
    name = str(tags.get("name") or tags.get("ref") or f"OSM {route_tag} route {element['id']}")
    raw: dict[str, str | int | float | bool | None] = {}
    for key in ("route", "network", "ref", "operator", "website", "name", "distance"):
        value = tags.get(key)
        if isinstance(value, (str, int, float, bool)):
            raw[key] = value
    member_tags: list[dict[str, Any]] = []
    members = element.get("members", [])
    if isinstance(members, list):
        for member in members:
            if (
                isinstance(member, dict)
                and member.get("type") == "way"
                and isinstance(member.get("tags"), dict)
            ):
                member_tags.append(member["tags"])
    if any(
        str(tags.get(key, "")).casefold() in {"yes", "true", "1"}
        for tags in member_tags
        for key in ("bridge", "tunnel")
    ):
        raw["bridge_tunnel"] = "mixed_or_present"
    layer_values = {str(tags.get("layer")) for tags in member_tags if "layer" in tags}
    if member_tags and len(layer_values) == 1 and all("layer" in tags for tags in member_tags):
        raw["layer"] = next(iter(layer_values))
    warnings = []
    if not lines:
        warnings.append("Route relation has no usable member-way geometry.")
    elif quality != "complete":
        warnings.append(
            "Some relation members could not be reconstructed; geometry preserves disconnected segments without bridging gaps."
        )
    if tags.get("distance") and distance is None:
        warnings.append(
            "Provider distance was present but could not be validated; distance remains unknown."
        )
    route = Route(
        id=f"osm:relation:{element['id']}",
        provider="osm",
        provider_route_id=str(element["id"]),
        source_url=f"https://www.openstreetmap.org/relation/{element['id']}",
        source_type=SourceType.official_trail,
        attribution=OSM_ATTRIBUTION,
        name=name[:240],
        description=str(tags["description"])[:1000] if tags.get("description") else None,
        activity=activity,
        geometry=geometry,
        start_lat=start[1] if start else None,
        start_lon=start[0] if start else None,
        end_lat=end[1] if end else None,
        end_lon=end[0] if end else None,
        distance_m=distance,
        elevation_gain_m=None,
        is_loop=loop,
        fetched_at=fetched_at or now(),
        geometry_quality=quality,
        gpx_access="unverified",
        warnings=warnings,
        raw_metadata=raw,
    )
    return route


def reconstruct_members(
    members: Any,
) -> tuple[list[list[tuple[float, float]]], Literal["complete", "disconnected", "incomplete"]]:
    """Join relation member ways in sequence, respecting role and endpoint direction.

    A gap starts a new line; coordinates are never fabricated to bridge it.
    """
    if not isinstance(members, list):
        return [], "incomplete"
    lines: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    incomplete = False
    disconnected = False
    saw_way = False
    for member in members:
        if not isinstance(member, dict):
            incomplete = True
            continue
        if member.get("type") == "relation":
            incomplete = True
            continue
        if member.get("type") != "way":
            continue
        saw_way = True
        geometry = member.get("geometry")
        if not isinstance(geometry, list):
            incomplete = True
            continue
        coords: list[tuple[float, float]] = []
        for point in geometry:
            if not isinstance(point, dict):
                continue
            try:
                lon, lat = float(point["lon"]), float(point["lat"])
                if (
                    math.isfinite(lon)
                    and math.isfinite(lat)
                    and -180 <= lon <= 180
                    and -90 <= lat <= 90
                ):
                    coords.append((lon, lat))
            except (TypeError, ValueError, KeyError):
                continue
        if len(coords) < 2:
            incomplete = True
            continue
        if member.get("role") == "backward":
            coords.reverse()
        if not current:
            current = coords
            continue
        if _same_point(current[-1], coords[0]):
            current.extend(coords[1:])
        elif _same_point(current[-1], coords[-1]):
            coords.reverse()
            current.extend(coords[1:])
        else:
            lines.append(current)
            current = coords
            disconnected = True
    if current:
        lines.append(current)
    lines = [line for line in lines if len(line) >= 2]
    if not saw_way:
        incomplete = True
    quality: Literal["complete", "disconnected", "incomplete"] = (
        "incomplete" if incomplete else ("disconnected" if disconnected else "complete")
    )
    return lines, quality


def _same_point(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) <= 1e-7 and abs(a[1] - b[1]) <= 1e-7


def _lines_length(lines: list[list[tuple[float, float]]]) -> float:
    total = 0.0
    for line in lines:
        total += sum(
            abs(float(GEOD.inv(a[0], a[1], b[0], b[1])[2]))
            for a, b in zip(line, line[1:], strict=False)
        )
    return round(total, 1)


def _activity(tags: dict[str, Any]) -> Activity:
    route = str(tags.get("route", "")).lower()
    network = str(tags.get("network", "")).lower()
    if route in {"hiking", "foot"}:
        return Activity.hiking
    if route == "running":
        return Activity.trail_running
    if route == "mtb" or "mtb" in network or str(tags.get("bicycle", "")).lower() == "mtb":
        return Activity.mtb
    if route == "bicycle":
        return (
            Activity.gravel
            if str(tags.get("surface", "")).lower() in {"gravel", "ground", "fine_gravel"}
            else Activity.road
        )
    return Activity.other


def activity_matches(requested: Activity, mapped: Activity) -> bool:
    if requested == mapped:
        return True
    if requested in {Activity.walking, Activity.hiking}:
        return mapped == Activity.hiking
    if requested in {Activity.running, Activity.trail_running}:
        return mapped in {Activity.trail_running, Activity.hiking}
    if requested == Activity.gravel:
        return mapped in {Activity.gravel, Activity.road, Activity.mtb}
    if requested == Activity.road:
        return mapped in {Activity.road, Activity.gravel}
    return False
