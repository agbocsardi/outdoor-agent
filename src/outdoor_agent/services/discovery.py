"""Live route discovery orchestration with explicit source limits and explanations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pyproj import Geod

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.closures import Assessment, ClosureReport, ClosureSourceError
from outdoor_agent.models.routes import Activity, Coordinate, Model, Route, SourceType
from outdoor_agent.models.time import parse_window
from outdoor_agent.providers.base import RouteDiscoveryProvider
from outdoor_agent.providers.base import SearchCriteria as ProviderSearchCriteria
from outdoor_agent.providers.geocoding import geocode
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.local import PROVENANCE_WARNING, LocalCatalogProvider
from outdoor_agent.providers.ndw import FeedFetchResult, fetch_feeds, parse_fixture
from outdoor_agent.providers.osm import OSMDiscoveryAdapter, OSMProvider
from outdoor_agent.providers.registry import registry
from outdoor_agent.services.ranking import SourceProvenance, rank_routes
from outdoor_agent.services.validation import assess_route

SearchSourceType = Literal["community", "official_trail", "all"]
DiscoveryAdapterFactory = Callable[[Settings, HTTPClient, SearchSourceType], RouteDiscoveryProvider]
DISCOVERY_ADAPTER_FACTORIES: dict[str, DiscoveryAdapterFactory] = {
    "osm": lambda settings, http, source_type: OSMDiscoveryAdapter(OSMProvider(settings, http)),
    "local": lambda settings, http, source_type: LocalCatalogProvider(
        settings, source_type=source_type
    ),
}


class SearchCriteria(Model):
    near: str | None = None
    radius_km: float = Field(default=25, gt=0, le=100)
    activity: Activity = Activity.hiking
    distance_min_m: float | None = Field(default=None, ge=0)
    distance_max_m: float | None = Field(default=None, ge=0)
    loop: Literal["preferred", "required", "any"] = "any"
    shape: Literal["any", "loop", "point_to_point"] = "any"
    bbox: tuple[float, float, float, float] | None = None
    date: str | None = None
    feed_file: Path | None = None
    limit: int = Field(default=10, ge=1, le=100)
    source_type: SearchSourceType = "all"
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
        if self.bbox is None and self.near is None and self.center is None:
            raise ValueError("Supply near, center or bbox for route discovery")
        if self.feed_file is not None and self.date is None:
            raise ValueError("feed_file is only valid with a search date")
        if self.loop == "required" and self.shape == "point_to_point":
            raise ValueError("loop=required conflicts with shape=point_to_point")
        if self.bbox is not None:
            south, west, north, east = self.bbox
            if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
                raise ValueError("bbox must be a non-wrapping WGS84 south,west,north,east tuple")
            center = self.center or ((west + east) / 2, (south + north) / 2)
            geod = Geod(ellps="WGS84")
            if (
                max(
                    abs(float(geod.inv(center[0], center[1], lon, lat)[2]))
                    for lon in (west, east)
                    for lat in (south, north)
                )
                > 100_000
            ):
                raise ValueError("bbox must fit within 100 km of its center")
            if self.center is None and self.near is None:
                self.center = center
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


class ClosureAssessmentSummary(Model):
    date: str
    status: Assessment
    source_status: Literal["ok", "partial", "failed", "fixture"]
    stale: bool
    unknown_coverage: bool
    warnings: list[str]
    confidence: Literal["high", "moderate", "low", "unknown"]


class RankedRoute(Model):
    route: Route
    ranking: RankingExplanation
    aliases: list[str] = Field(default_factory=list)
    provenance: list[SourceProvenance] = Field(default_factory=list)
    closure_assessment: ClosureAssessmentSummary | None = None


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
    """Search mapped trails or the explicitly selected private saved-route catalog.

    ``criteria.providers`` defaults to OSM. Community search raises a typed
    unavailable error unless the saved local catalog is selected; no configured
    public community adapter is verified and fixtures are never substituted.
    """
    if criteria.date:
        try:
            parse_window(criteria.date, settings.default_timezone)
        except (ValueError, TypeError) as exc:
            raise OutdoorError(
                "INVALID_DATE",
                "Search date/time must be ISO 8601; ambiguous local times need an explicit UTC offset.",
                provider="discovery",
            ) from exc
    if (
        criteria.activity == Activity.other
        and "osm" in criteria.providers
        and criteria.source_type != "community"
    ):
        raise OutdoorError(
            "ACTIVITY_UNSUPPORTED",
            "OSM route-relation discovery does not cover the requested other activity.",
            5,
            provider="osm",
            suggestions=[
                "Choose a supported hiking, walking, running, road, gravel, or MTB activity."
            ],
        )
    declared = {item.provider: item for item in registry()}
    if criteria.source_type == "community" and (
        "local" not in criteria.providers
        or "local" in settings.disabled_providers
        or "local" not in declared
        or declared["local"].capabilities.get("discover_saved") is None
        or declared["local"].capabilities["discover_saved"].state.value != "supported"
    ):
        raise OutdoorError(
            "COMMUNITY_DISCOVERY_UNAVAILABLE",
            "No public community catalog is configured. Select the local saved catalog and import an entitled GPX with community attribution.",
            5,
            provider="discovery",
            suggestions=[
                "Use --provider local and import an entitled GPX with --source-type community --attribution."
            ],
        )
    available = [
        name
        for name in criteria.providers
        if name in DISCOVERY_ADAPTER_FACTORIES
        and name not in settings.disabled_providers
        and name in declared
        and declared[name].capabilities.get(
            "discover_saved" if name == "local" else "discover_public"
        )
        is not None
        and declared[name]
        .capabilities["discover_saved" if name == "local" else "discover_public"]
        .state.value
        == "supported"
    ]
    if not available:
        if criteria.source_type == "community":
            raise OutdoorError(
                "COMMUNITY_DISCOVERY_UNAVAILABLE",
                "No public community catalog is configured. Select the local saved catalog and import an entitled GPX with community attribution.",
                5,
                provider="discovery",
            )
        raise OutdoorError(
            "PROVIDER_UNSUPPORTED",
            "None of the selected providers supports route discovery.",
            5,
            provider="discovery",
            suggestions=["Select a provider with verified public discovery support."],
        )
    center = criteria.center
    if criteria.bbox is not None:
        bounds = criteria.bbox
        if center is None and criteria.near is not None:
            center = await geocode(settings, criteria.near)
        if center is None:
            south, west, north, east = bounds
            center = ((west + east) / 2, (south + north) / 2)
    else:
        if center is None:
            assert criteria.near is not None
            center = await geocode(settings, criteria.near)
        bounds = _bounds(center, criteria.radius_km)
    assert center is not None
    warnings: list[str] = []
    statuses: list[DiscoveryProviderStatus] = []
    routes: list[Route] = []
    provider_errors: list[OutdoorError] = []
    query_succeeded = False
    for provider_name in criteria.providers:
        if criteria.source_type == "community" and provider_name != "local":
            continue
        factory = DISCOVERY_ADAPTER_FACTORIES.get(provider_name)
        if factory is None:
            statuses.append(
                DiscoveryProviderStatus(
                    provider=provider_name,
                    status="unsupported",
                    reason="Public discovery is not implemented for this provider.",
                )
            )
            warnings.append(
                f"{provider_name} public discovery is unavailable; no substitute results were used."
            )
            continue
        if provider_name in settings.disabled_providers:
            statuses.append(
                DiscoveryProviderStatus(
                    provider=provider_name,
                    status="disabled",
                    reason="Disabled in configuration",
                )
            )
            continue
        try:
            async with HTTPClient(settings) as http:
                adapter = factory(settings, http, criteria.source_type)
                capability_name = (
                    "discover_saved" if provider_name == "local" else "discover_public"
                )
                if (
                    adapter.declaration.capabilities.get(capability_name) is None
                    or adapter.declaration.capabilities[capability_name].state.value != "supported"
                ):
                    statuses.append(
                        DiscoveryProviderStatus(
                            provider=provider_name,
                            status="unsupported",
                            reason="Discovery capability is not supported.",
                        )
                    )
                    continue
                provider_result = await adapter.search(
                    ProviderSearchCriteria(bbox=bounds, activity=criteria.activity, limit=200)
                )
            query_succeeded = True
            provider_routes = [
                route
                for route in provider_result.routes
                if (criteria.source_type == "all" and route.source_type != SourceType.generated)
                or route.source_type.value == criteria.source_type
            ]
            routes.extend(provider_routes)
            stale = provider_result.stale
            cached = provider_result.cached
            partial_response = bool(provider_result.warnings)
            reasons = []
            if stale:
                reasons.append("stale cache")
            if partial_response:
                reasons.append("partial provider response")
            statuses.append(
                DiscoveryProviderStatus(
                    provider=provider_name,
                    status="degraded" if reasons else "ok",
                    result_count=len(provider_routes),
                    reason="Using " + " and ".join(reasons) + "." if reasons else None,
                    cached=cached,
                    stale=stale,
                )
            )
            if stale:
                warnings.append(
                    f"{provider_name} results came from stale cache; verify against the source before relying on them."
                )
            for route in provider_routes:
                warnings.extend(
                    f"{route.name}: {warning}"
                    for warning in route.warnings
                    if not (provider_name == "local" and warning == PROVENANCE_WARNING)
                )
            warnings.extend(provider_result.warnings)
        except OutdoorError as exc:
            provider_errors.append(exc)
            statuses.append(
                DiscoveryProviderStatus(provider=provider_name, status="degraded", reason=exc.code)
            )
            warnings.append(f"{provider_name} route discovery failed: {exc.message}")
    if not query_succeeded:
        local_empty = next(
            (error for error in provider_errors if error.code == "LOCAL_CATALOG_EMPTY"), None
        )
        if local_empty is not None:
            raise local_empty
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
    if criteria.source_type == "all" and any(name != "local" for name in criteria.providers):
        warnings.append(
            "No verified public community-route catalog is configured; public results are mapped OSM official trails. Any saved community provenance is user-supplied."
        )
    closure_reports: dict[str, ClosureReport] = {}
    if criteria.date:
        eligible = rank_routes(
            routes,
            activity=criteria.activity,
            center=center,
            radius_km=criteria.radius_km,
            distance_min_m=criteria.distance_min_m,
            distance_max_m=criteria.distance_max_m,
            loop=criteria.loop,
            weights=settings.ranking_weights,
            shape=criteria.shape,
        )
        candidate_routes = [candidate.route for candidate in eligible[:200]]
        # Do not fetch/decode closure feeds if no route can pass the user's filters.
        if candidate_routes:
            if criteria.feed_file is not None:
                try:
                    feed_result = FeedFetchResult(
                        snapshots=[
                            parse_fixture(criteria.feed_file, timezone=settings.default_timezone)
                        ],
                        errors=[],
                    )
                except (OSError, ValueError) as exc:
                    raise OutdoorError(
                        "INVALID_CLOSURE_FIXTURE",
                        "The supplied closure fixture is missing or invalid.",
                        provider="ndw",
                    ) from exc
            else:
                try:
                    feed_result = await fetch_feeds(settings)
                except Exception as exc:
                    feed_result = FeedFetchResult(
                        snapshots=[],
                        errors=[
                            ClosureSourceError(
                                source="ndw",
                                code=getattr(exc, "code", "PROVIDER_UNAVAILABLE"),
                                message="NDW closure feeds are unavailable.",
                            )
                        ],
                    )
            for route in candidate_routes:
                try:
                    closure_reports[route.id] = await assess_route(
                        settings,
                        route,
                        criteria.date,
                        criteria.activity,
                        feed_file=criteria.feed_file,
                        feed_result=feed_result,
                    )
                except OutdoorError as exc:
                    warnings.append(f"Closure assessment for {route.name} failed: {exc.message}")
                    closure_reports[route.id] = ClosureReport(
                        route_id=route.id,
                        activity=criteria.activity.value,
                        requested_date=criteria.date,
                        status="check_failed",
                        source_status="failed",
                        unknown_coverage=True,
                        warnings=[f"Closure assessment failed: {exc.message}"],
                    )
        for report in closure_reports.values():
            warnings.extend(report.warnings)
            if report.unknown_coverage or report.stale or report.source_status != "ok":
                warnings.append(
                    f"Closure assessment for {report.route_id} is partial or has unknown coverage."
                )
    ranking_weights = dict(settings.ranking_weights)
    if criteria.date:
        ranking_weights.setdefault("closures", 1.0)
    ranked = rank_routes(
        routes,
        activity=criteria.activity,
        center=center,
        radius_km=criteria.radius_km,
        distance_min_m=criteria.distance_min_m,
        distance_max_m=criteria.distance_max_m,
        loop=criteria.loop,
        weights=ranking_weights,
        closure_reports=closure_reports,
        shape=criteria.shape,
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
                closure_assessment=(
                    ClosureAssessmentSummary(
                        date=item.closure.requested_date,
                        status=item.closure.status,
                        source_status=item.closure.source_status,
                        stale=item.closure.stale,
                        unknown_coverage=item.closure.unknown_coverage,
                        warnings=item.closure.warnings,
                        confidence=(
                            "high"
                            if item.closure.status == "known_conflict" and not item.closure.stale
                            else "moderate"
                            if item.closure.status in {"known_conflict", "possible_conflict"}
                            else "unknown"
                            if item.closure.unknown_coverage
                            or item.closure.status in {"coverage_unknown", "check_failed"}
                            else "low"
                        ),
                    )
                    if item.closure
                    else None
                ),
            )
            for item in ranked
        ],
        provider_status=statuses,
        warnings=warnings,
        partial=bool(warnings) or any(status.status != "ok" for status in statuses),
    )
