"""Live route discovery orchestration with explicit source limits and explanations."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator
from pyproj import Geod

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Activity, Coordinate, Model, Route
from outdoor_agent.providers.geocoding import geocode
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.osm import OSMProvider
from outdoor_agent.providers.registry import registry
from outdoor_agent.services.ranking import SourceProvenance, rank_routes


class SearchCriteria(Model):
    near: str
    radius_km: float = Field(default=25, gt=0, le=100)
    activity: Activity = Activity.hiking
    distance_min_m: float | None = Field(default=None, ge=0)
    distance_max_m: float | None = Field(default=None, ge=0)
    loop: Literal["preferred", "required", "any"] = "any"
    limit: int = Field(default=10, ge=1, le=100)
    source_type: Literal["community", "official_trail", "all"] = "all"
    providers: list[str] = Field(default_factory=lambda: ["osm"])
    center: Coordinate | None = None

    @model_validator(mode="after")
    def valid_distance_range(self) -> SearchCriteria:
        if (
            self.distance_min_m is not None
            and self.distance_max_m is not None
            and self.distance_min_m > self.distance_max_m
        ):
            raise ValueError("Minimum route length must not exceed maximum route length")
        return self

    @field_validator("providers")
    @classmethod
    def known_provider_names(cls, value: list[str]) -> list[str]:
        known = {item.provider for item in registry()} | {"alltrails", "strava"}
        if not value or len(set(value)) != len(value) or any(item not in known for item in value):
            raise ValueError("Providers must be a nonempty list of unique known providers")
        return value


class RankingExplanation(Model):
    score: float
    factors: dict[str, float]
    explanations: list[str]


class RankedRoute(Model):
    route: Route
    ranking: RankingExplanation
    aliases: list[str] = Field(default_factory=list)
    provenance: list[SourceProvenance] = Field(default_factory=list)


class DiscoveryProviderStatus(Model):
    provider: str
    status: Literal["ok", "unsupported", "degraded", "disabled"]
    result_count: int = 0
    reason: str | None = None
    cached: bool = False
    stale: bool = False


class DiscoveryResult(Model):
    routes: list[RankedRoute]
    total: int = Field(default=0, ge=0)
    provider_status: list[DiscoveryProviderStatus]
    warnings: list[str] = Field(default_factory=list)
    partial: bool = False

    @model_validator(mode="after")
    def result_count(self) -> DiscoveryResult:
        self.total = len(self.routes)
        return self

    @property
    def normalized_routes(self) -> list[Route]:
        """Convenience for callers that persist the normalized route models."""
        return [item.route for item in self.routes]


def _bounds(center: Coordinate, radius_km: float) -> tuple[float, float, float, float]:
    lon, lat = center
    geod = Geod(ellps="WGS84")
    radius_m = radius_km * 1000
    west_lon, _, _ = geod.fwd(lon, lat, 270, radius_m)
    east_lon, _, _ = geod.fwd(lon, lat, 90, radius_m)
    _, south, _ = geod.fwd(lon, lat, 180, radius_m)
    _, north, _ = geod.fwd(lon, lat, 0, radius_m)
    west_lon = (west_lon + 180) % 360 - 180
    east_lon = (east_lon + 180) % 360 - 180
    if west_lon > east_lon:
        raise OutdoorError(
            "ANTIMERIDIAN_UNSUPPORTED",
            "This Overpass query cannot represent a radius crossing the antimeridian.",
            5,
            provider="osm",
            suggestions=["Choose a center and radius that remain on one side of the antimeridian."],
        )
    return (
        max(-90.0, min(south, north)),
        west_lon,
        min(90.0, max(south, north)),
        east_lon,
    )


async def search_routes(settings: Settings, criteria: SearchCriteria) -> DiscoveryResult:
    """Search mapped official trails and return scored routes with source warnings.

    ``criteria.providers`` defaults to OSM. Community search raises a typed
    unavailable error because the configured public catalog has no verified
    discovery adapter; this function never substitutes fixtures.
    """
    if criteria.source_type == "community":
        raise OutdoorError(
            "COMMUNITY_DISCOVERY_UNAVAILABLE",
            "No configured provider currently offers verified public community-route discovery.",
            5,
            provider="discovery",
            suggestions=[
                "Search official mapped OSM trails, or import a route from a source you can access."
            ],
        )
    if criteria.activity == Activity.other and "osm" in criteria.providers:
        raise OutdoorError(
            "ACTIVITY_UNSUPPORTED",
            "OSM route-relation discovery does not cover the requested other activity.",
            5,
            provider="osm",
            suggestions=[
                "Choose a supported hiking, walking, running, road, gravel, or MTB activity."
            ],
        )
    if "osm" not in criteria.providers or "osm" in settings.disabled_providers:
        raise OutdoorError(
            "PROVIDER_UNSUPPORTED",
            "None of the selected providers supports route discovery.",
            5,
            provider="discovery",
            suggestions=["Select OSM for mapped trails or import a legally obtained GPX."],
        )
    center = criteria.center or await geocode(settings, criteria.near)
    warnings: list[str] = []
    statuses: list[DiscoveryProviderStatus] = []
    routes: list[Route] = []
    query_succeeded = False
    if "osm" in criteria.providers:
        try:
            async with HTTPClient(settings) as http:
                osm = OSMProvider(settings, http)
                routes = await osm.search(
                    _bounds(center, criteria.radius_km), activity=criteria.activity, limit=200
                )
                fetch = osm.last_fetch
            query_succeeded = True
            stale = bool(fetch and fetch.stale)
            cached = bool(fetch and fetch.from_cache)
            partial_response = bool(osm.warnings)
            reasons = []
            if stale:
                reasons.append("stale cache")
            if partial_response:
                reasons.append("partial Overpass response")
            statuses.append(
                DiscoveryProviderStatus(
                    provider="osm",
                    status="degraded" if reasons else "ok",
                    result_count=len(routes),
                    reason="Using " + " and ".join(reasons) + "." if reasons else None,
                    cached=cached,
                    stale=stale,
                )
            )
            if stale:
                warnings.append(
                    "OSM results came from stale cache; verify against the source before relying on them."
                )
            for route in routes:
                warnings.extend(f"{route.name}: {warning}" for warning in route.warnings)
            warnings.extend(osm.warnings)
        except OutdoorError as exc:
            statuses.append(
                DiscoveryProviderStatus(provider="osm", status="degraded", reason=exc.code)
            )
            warnings.append(f"OSM route discovery failed: {exc.message}")
    else:
        statuses.append(
            DiscoveryProviderStatus(provider="osm", status="disabled", reason="Not selected")
        )
    for provider in criteria.providers:
        if provider != "osm":
            statuses.append(
                DiscoveryProviderStatus(
                    provider=provider,
                    status="unsupported",
                    reason="Public community discovery is not verified or implemented.",
                )
            )
            warnings.append(
                f"{provider} public community-route discovery is unavailable; no substitute results were used."
            )
    if not query_succeeded:
        if all(status.status in {"unsupported", "disabled"} for status in statuses):
            raise OutdoorError(
                "PROVIDER_UNSUPPORTED",
                "None of the selected providers supports route discovery.",
                5,
                provider="discovery",
                suggestions=[
                    "Select OSM for mapped official trails, or import a route from a provider you can access."
                ],
            )
        raise OutdoorError(
            "PROVIDERS_UNAVAILABLE",
            "All selected route discovery providers failed or are unsupported.",
            4,
            provider="discovery",
            retryable=True,
            suggestions=["Retry later or select another configured discovery provider."],
        )
    if criteria.source_type == "all":
        warnings.append(
            "No verified public community-route catalog is configured; results are mapped OSM official trails."
        )
    ranked = rank_routes(
        routes,
        activity=criteria.activity,
        center=center,
        radius_km=criteria.radius_km,
        distance_min_m=criteria.distance_min_m,
        distance_max_m=criteria.distance_max_m,
        loop=criteria.loop,
        weights=settings.ranking_weights,
    )[: criteria.limit]
    return DiscoveryResult(
        routes=[
            RankedRoute(
                route=item.route,
                ranking=RankingExplanation(
                    score=item.score, factors=item.factors, explanations=item.explanations
                ),
                aliases=item.aliases,
                provenance=item.provenance,
            )
            for item in ranked
        ],
        provider_status=statuses,
        warnings=warnings,
        partial=bool(warnings) or any(status.status != "ok" for status in statuses),
    )
