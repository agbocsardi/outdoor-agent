"""Place enrichment orchestration independent from provider HTTP details."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.places import NearbyPlace, PlacesResult
from outdoor_agent.models.routes import Route
from outdoor_agent.providers.base import PlaceEnrichmentProvider
from outdoor_agent.providers.google_places import GooglePlacesProvider
from outdoor_agent.providers.registry import CapabilityState

if TYPE_CHECKING:
    from outdoor_agent.config import Settings
    from outdoor_agent.providers.http import HTTPClient


class _DetailedPlaceProvider(PlaceEnrichmentProvider, Protocol):
    last_result: PlacesResult | None


PlaceProviderFactory = Callable[["Settings", "HTTPClient | None"], _DetailedPlaceProvider]


def _google_factory(
    settings: "Settings", http_client: "HTTPClient | None"
) -> _DetailedPlaceProvider:
    return GooglePlacesProvider(settings, http_client=http_client)


# Only adapters with a concrete implementation and typed result details belong here.
PLACE_PROVIDERS: dict[str, PlaceProviderFactory] = {"google": _google_factory}


async def enrich_places(
    settings: "Settings",
    route: Route,
    category: str,
    provider: str = "google",
    *,
    http_client: "HTTPClient | None" = None,
) -> PlacesResult:
    """Enrich one route with the selected typed provider, preserving its detailed result."""
    factory = PLACE_PROVIDERS.get(provider)
    if factory is None:
        raise OutdoorError(
            "PROVIDER_UNSUPPORTED", "Place enrichment supports the Google provider only.", 2
        )

    adapter = factory(settings, http_client)
    if (
        not adapter.declaration.implemented
        or not adapter.declaration.enabled
        or "places" not in adapter.declaration.capabilities
    ):
        raise OutdoorError(
            "PROVIDER_UNSUPPORTED",
            "Selected provider is disabled or does not declare place enrichment capability.",
            2,
            provider=provider,
        )
    capability = adapter.declaration.capabilities["places"]
    if capability.state == CapabilityState.unsupported:
        raise OutdoorError(
            "PROVIDER_UNSUPPORTED",
            "Selected provider does not support place enrichment.",
            2,
            provider=provider,
        )

    if provider == "google" and (
        not settings.google_enabled or provider in settings.disabled_providers
    ):
        raise OutdoorError(
            "GOOGLE_DISABLED",
            "Google Places enrichment is disabled; enable it explicitly in configuration.",
            5,
            provider="google",
        )
    if route.geometry is None:
        raise OutdoorError(
            "ROUTE_GEOMETRY_REQUIRED",
            "Route geometry is required for near-route place enrichment.",
            2,
            provider=provider,
        )

    await adapter.near_route(route.geometry, category)
    if adapter.last_result is None:
        raise OutdoorError(
            "PROVIDER_RESULT_MISSING",
            "Place provider completed without returning a typed result.",
            4,
            provider=provider,
        )
    return adapter.last_result


__all__ = ["NearbyPlace", "PLACE_PROVIDERS", "PlacesResult", "enrich_places"]
