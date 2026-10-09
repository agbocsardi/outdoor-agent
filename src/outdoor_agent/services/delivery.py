"""Lawful GPX export and manual COROS handoff; no private account write APIs."""

import hashlib
from collections.abc import Callable
from pathlib import Path

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.export import write_gpx
from outdoor_agent.geo.gpx import MAX_BYTES, inspect_gpx
from outdoor_agent.models.delivery import ExportResult as ExportResult
from outdoor_agent.models.delivery import HandoffResult as HandoffResult
from outdoor_agent.models.routes import Route
from outdoor_agent.providers.base import (
    ApprovedRequest,
    DeliveryProvider,
    RouteDetailProvider,
    RouteExportProvider,
)
from outdoor_agent.providers.coros import ManualCorosProvider
from outdoor_agent.providers.gpx_export import CachedGeometryExporter
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.osm import OSMProvider
from outdoor_agent.providers.registry import registry
from outdoor_agent.providers.ridewithgps import RideWithGPS
from outdoor_agent.storage.db import RouteStore

DetailProviderFactory = Callable[[Settings, HTTPClient], RouteDetailProvider]
ExportProviderFactory = Callable[[Settings, HTTPClient, Route], RouteExportProvider]
DETAIL_PROVIDER_FACTORIES: dict[str, DetailProviderFactory] = {
    "osm": lambda settings, http: OSMProvider(settings, http),
    "ridewithgps": lambda settings, http: RideWithGPS(settings, http),
}
EXPORT_PROVIDER_FACTORIES: dict[str, ExportProviderFactory] = {
    "osm": lambda settings, http, route: CachedGeometryExporter(route),
    "ridewithgps": lambda settings, http, route: RideWithGPS(settings, http),
    "local": lambda settings, http, route: CachedGeometryExporter(route),
}
EXPORT_ORIGINAL_BYTES = {"osm": False, "ridewithgps": True, "local": False}


async def load_route(settings: Settings, route_id: str, *, refresh: bool = False) -> Route:
    store = RouteStore(settings.data_dir)
    if not refresh:
        try:
            return store.get_route(route_id)
        except OutdoorError as exc:
            if exc.code != "ROUTE_NOT_FOUND":
                raise
    selected_provider = route_id.split(":", 1)[0]
    if selected_provider in settings.disabled_providers:
        raise OutdoorError(
            "PROVIDER_DISABLED",
            "This provider is disabled in configuration.",
            5,
            provider=selected_provider,
        )
    factory = DETAIL_PROVIDER_FACTORIES.get(selected_provider)
    if factory is None:
        raise OutdoorError(
            "ROUTE_NOT_FOUND",
            "Unknown or unavailable route ID; import a GPX or search a supported provider first.",
        )
    declaration = next((item for item in registry() if item.provider == selected_provider), None)
    capability = declaration.capabilities.get("get_metadata") if declaration else None
    if capability is None or capability.state.value in {"unsupported", "unverified"}:
        raise OutdoorError(
            "PROVIDER_UNSUPPORTED",
            "Provider route detail is not available or verified.",
            5,
            provider=selected_provider,
        )
    async with HTTPClient(settings) as http:
        provider = factory(settings, http)
        route = await provider.get(route_id)
    store.save_route(route)
    return route


async def export_route(
    settings: Settings, route_id: str, path: Path, *, overwrite: bool = False
) -> ExportResult:
    store = RouteStore(settings.data_dir)
    route = await load_route(settings, route_id)
    content = store.artifact_bytes(route.id)
    original = store.artifact_original(route.id) if content is not None else False
    warnings: list[str] = list(route.warnings)
    if content is None:
        export_factory = EXPORT_PROVIDER_FACTORIES.get(route.provider)
        if export_factory is None:
            raise OutdoorError(
                "EXPORT_UNSUPPORTED",
                "No GPX export adapter is available for this route provider.",
                5,
                provider=route.provider,
            )
        if route.provider in settings.disabled_providers:
            raise OutdoorError(
                "PROVIDER_DISABLED",
                "This provider is disabled in configuration.",
                5,
                provider=route.provider,
            )
        async with HTTPClient(settings) as http:
            exporter = export_factory(settings, http, route)
            artifact = await exporter.export_gpx(route_id)
        artifact_path = Path(artifact.path)
        try:
            with artifact_path.open("rb") as file:
                content = file.read(MAX_BYTES + 1)
        except OSError as exc:
            raise OutdoorError(
                "PROVIDER_ARTIFACT_INVALID",
                "Provider GPX artifact could not be read.",
                4,
                provider=route.provider,
            ) from exc
        finally:
            artifact_path.unlink(missing_ok=True)
        if (
            artifact.route_id != route.id
            or len(content) != artifact.byte_count
            or len(content) > MAX_BYTES
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise OutdoorError(
                "PROVIDER_ARTIFACT_INVALID",
                "Provider GPX artifact metadata does not match its bytes.",
                4,
                provider=route.provider,
            )
        inspect_gpx(content)
        original = EXPORT_ORIGINAL_BYTES[route.provider]
        if original:
            route = route.model_copy(update={"gpx_access": "available"})
        store.save_route(route, content, content_original=original)
    if not original:
        warnings.append(
            "GPX encodes the existing mapped source geometry; it is not an original provider GPX. Gaps remain separate segments."
        )
    write_gpx(path, content, overwrite=overwrite)
    return ExportResult(
        route_id=route.id,
        path=str(path.absolute()),
        sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
        original_bytes=original,
        source_url=route.source_url,
        attribution=route.attribution,
        warnings=warnings,
    )


async def prepare_handoff(
    settings: Settings,
    route_id: str,
    *,
    target: str = "coros",
    confirm: bool = False,
    dry_run: bool = False,
    output: Path | None = None,
    account: str | None = None,
) -> HandoffResult:
    """Prepare a reviewable plan; commit only the explicitly confirmed request."""
    factories: dict[str, Callable[[], ManualCorosProvider]] = {
        "coros": lambda: ManualCorosProvider(
            settings, export_route, output=output, account=account
        ),
    }
    factory = factories.get(target)
    if factory is None:
        raise OutdoorError(
            "DELIVERY_TARGET_UNSUPPORTED",
            "Only the documented manual COROS GPX handoff is available.",
            5,
        )
    if target in settings.disabled_providers:
        raise OutdoorError(
            "PROVIDER_DISABLED",
            "This delivery provider is disabled in configuration.",
            5,
            provider=target,
        )
    if dry_run and confirm:
        raise OutdoorError("INVALID_ARGUMENTS", "Choose --dry-run or --confirm, not both.")
    route = RouteStore(settings.data_dir).get_route(route_id)
    adapter = factory()
    provider: DeliveryProvider = adapter
    await provider.prepare(route)
    assert adapter.last_result is not None
    if confirm:
        await provider.commit(
            ApprovedRequest(
                route_id=route_id,
                target=target,
                account=account or "manual",
                confirmed=True,
                idempotency_key=adapter.last_result.idempotency_key,
            )
        )
    assert adapter.last_result is not None
    return adapter.last_result
