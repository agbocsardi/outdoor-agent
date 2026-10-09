"""Lawful GPX export and manual COROS handoff; no private account write APIs."""

import hashlib
import json
from pathlib import Path

from pydantic import Field

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.export import route_gpx, write_gpx
from outdoor_agent.models.routes import Model, Route
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.ridewithgps import RideWithGPS, numeric_route_id
from outdoor_agent.storage.db import RouteStore


class ExportResult(Model):
    status: str = "exported"
    route_id: str
    path: str
    sha256: str
    byte_count: int
    original_bytes: bool
    source_url: str | None
    attribution: str
    warnings: list[str] = Field(default_factory=list)


class HandoffResult(Model):
    route_id: str
    target: str = "coros"
    account: str | None = None
    status: str
    dry_run: bool
    path: str
    source_url: str | None
    idempotency_key: str
    already_prepared: bool = False
    side_effects: list[str]
    instructions: list[str]
    watch_status: str = "watch_sync_unverified"
    warnings: list[str] = Field(default_factory=list)


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
    if route_id.startswith("ridewithgps:"):
        async with HTTPClient(settings) as http:
            route = await RideWithGPS(settings, http).get(route_id)
    elif route_id.startswith("osm:"):
        from outdoor_agent.providers.osm import OSMProvider

        async with HTTPClient(settings) as http:
            route = await OSMProvider(settings, http).get(numeric_route_id(route_id, "osm"))
    else:
        raise OutdoorError(
            "ROUTE_NOT_FOUND",
            "Unknown or unavailable route ID; import a GPX or search a supported provider first.",
        )
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
        if route.provider == "ridewithgps":
            if "ridewithgps" in settings.disabled_providers:
                raise OutdoorError(
                    "PROVIDER_DISABLED",
                    "Ride with GPS is disabled in configuration.",
                    5,
                    provider="ridewithgps",
                )
            async with HTTPClient(settings) as http:
                content = await RideWithGPS(settings, http).export_bytes(route_id)
            original = True
            route = route.model_copy(update={"gpx_access": "available"})
            store.save_route(route, content)
        else:
            content = route_gpx(route)
            store.save_route(route, content, content_original=False)
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
    """Default to a read-only plan. Confirmation only prepares a local file for manual import."""
    if target != "coros":
        raise OutdoorError(
            "DELIVERY_TARGET_UNSUPPORTED",
            "Only the documented manual COROS GPX handoff is available.",
            5,
        )
    if dry_run and confirm:
        raise OutdoorError("INVALID_ARGUMENTS", "Choose --dry-run or --confirm, not both.")
    # Do not fetch a remote account or create local state during an unapproved plan.
    route = RouteStore(settings.data_dir).get_route(route_id)
    revision = hashlib.sha256(
        json.dumps(
            route.model_dump(mode="json", exclude={"fetched_at", "warnings", "gpx_access"}),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    path = (
        output
        or settings.data_dir
        / "handoff"
        / f"{hashlib.sha256((route_id + revision).encode()).hexdigest()[:24]}.gpx"
    )
    key = hashlib.sha256(
        f"{route_id}|{revision}|{target}|{account or 'manual'}|{path.absolute()}".encode()
    ).hexdigest()
    result = HandoffResult(
        route_id=route_id,
        target=target,
        account=account,
        status="manual_handoff_planned",
        dry_run=not confirm,
        path=str(path.absolute()),
        source_url=route.source_url,
        idempotency_key=key,
        side_effects=[
            "Write a local GPX artifact for manual import",
            "Record the local preparation in SQLite; no third-party account write",
        ],
        instructions=[
            "Open the GPX file on your phone and choose the COROS app.",
            "Save the route in the COROS app's collection.",
            "Choose Sync With Your Device in the COROS app; verify the route on the watch.",
        ],
        warnings=[
            "No supported automatic Ride with GPS pin/send action was verified. Nothing is submitted to COROS.",
            "GPX route navigation does not install offline map tiles.",
        ],
    )
    if not confirm:
        return result
    previous = RouteStore(settings.data_dir).delivery_record(key)
    if previous is not None and path.is_file():
        from outdoor_agent.geo.gpx import inspect_gpx, read_gpx

        digest = inspect_gpx(read_gpx(path)).sha256
        if digest == previous.get("artifact_sha256"):
            return result.model_copy(
                update={
                    "status": "manual_handoff_prepared",
                    "dry_run": False,
                    "already_prepared": True,
                }
            )
    exported = await export_route(settings, route_id, path)
    prepared = result.model_copy(
        update={
            "status": "manual_handoff_prepared",
            "dry_run": False,
            "warnings": result.warnings + exported.warnings,
        }
    )
    audit = prepared.model_dump(mode="json")
    audit["artifact_sha256"] = exported.sha256
    RouteStore(settings.data_dir).record_delivery(key, route_id, target, audit)
    return prepared
