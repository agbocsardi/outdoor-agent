"""Typed adapter boundaries; future I/O stays outside deterministic services."""

from typing import Protocol

from outdoor_agent.models.closures import ClosureEvent
from outdoor_agent.models.routes import Activity, Geometry, GPXArtifact, Model, Route
from outdoor_agent.providers.registry import Provider


class SearchCriteria(Model):
    activity: Activity
    bbox: tuple[float, float, float, float]
    limit: int = 10


class SearchResult(Model):
    routes: list[Route]
    warnings: list[str]


class ClosureFeedResult(Model):
    events: list[ClosureEvent]
    coverage: str
    warnings: list[str]


class RouteDiscoveryProvider(Protocol):
    declaration: Provider

    async def search(self, criteria: SearchCriteria) -> SearchResult: ...


class RouteDetailProvider(Protocol):
    declaration: Provider

    async def get(self, route_id: str) -> Route: ...


class RouteExportProvider(Protocol):
    declaration: Provider

    async def export_gpx(self, route_id: str) -> GPXArtifact: ...


class ClosureProvider(Protocol):
    declaration: Provider

    async def list_events(
        self, bbox: tuple[float, float, float, float], window: str, mode: str
    ) -> ClosureFeedResult: ...


class Place(Model):
    name: str
    source_url: str


class PlaceEnrichmentProvider(Protocol):
    declaration: Provider

    async def near_route(self, geometry: Geometry, kind: str) -> list[Place]: ...


class ApprovedRequest(Model):
    route_id: str
    target: str
    account: str
    confirmed: bool
    idempotency_key: str


class DeliveryPlan(Model):
    route_id: str
    target: str
    account: str | None
    side_effects: list[str]


class DeliveryProvider(Protocol):
    declaration: Provider

    async def prepare(self, route: Route) -> DeliveryPlan: ...

    async def commit(self, approved_request: ApprovedRequest) -> str: ...
