"""JSON-first outdoor route discovery, assessment and approved GPX handoff."""

import asyncio
import json
import os
import sqlite3
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

import click
import typer
from pydantic import BaseModel, ValidationError
from typer.core import TyperGroup

from outdoor_agent import __version__
from outdoor_agent.config import Settings, load_settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import inspect_gpx, read_gpx
from outdoor_agent.models.closures import ClosureEvent, ClosureFinding, ClosureReport
from outdoor_agent.models.delivery import ExportPlan
from outdoor_agent.models.responses import ErrorDetail, ProviderStatus, Response
from outdoor_agent.models.routes import Activity, GPXInspection, ImportResult, Route, SourceType
from outdoor_agent.models.time import parse_window
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.ndw import FeedFetchResult, fetch_feeds
from outdoor_agent.providers.registry import Provider, registry
from outdoor_agent.providers.ride_cli import RideCLI
from outdoor_agent.providers.ridewithgps import RideWithGPS
from outdoor_agent.services.delivery import (
    ExportResult,
    HandoffResult,
    export_route,
    load_route,
    prepare_handoff,
)
from outdoor_agent.services.discovery import DiscoveryResult, SearchCriteria, search_routes
from outdoor_agent.services.places import PlacesResult, enrich_places
from outdoor_agent.services.validation import assess_route
from outdoor_agent.storage.db import RouteStore


def emit(response: Response, as_json: bool = False, *, quiet: bool = False) -> None:
    """One object on stdout. Human diagnostics/errors use stderr."""
    if as_json:
        typer.echo(response.model_dump_json(exclude_none=True))
    elif response.status == "error":
        assert response.error is not None
        typer.echo(f"{response.error.code}: {response.error.message}", err=True)
        for suggestion in response.error.suggestions:
            typer.echo(f"  {suggestion}", err=True)
    else:
        typer.echo(json.dumps(response.data, indent=2, ensure_ascii=False))
        if not quiet:
            for warning in response.warnings:
                typer.echo(f"Warning: {warning}", err=True)


class JSONGroup(TyperGroup):
    """Wrap parser and service failures in the same public response contract."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        argv = kwargs.get("args")
        if argv is None:
            argv = args[0] if args else sys.argv[1:]
        as_json = "--json" in argv
        # Parser errors must reach this wrapper instead of Click printing raw input.
        kwargs["standalone_mode"] = False
        try:
            return super().main(*args, **kwargs)
        except (OutdoorError, click.ClickException) as exc:
            if isinstance(exc, OutdoorError):
                error = exc
            else:
                error = OutdoorError(
                    "INVALID_ARGUMENTS",
                    "Invalid command arguments. See outdoor-agent <command> --help.",
                )
            emit(
                Response(
                    status="error",
                    error=ErrorDetail(
                        code=error.code,
                        message=error.message,
                        provider=error.provider,
                        retryable=error.retryable,
                        suggestions=error.suggestions,
                    ),
                ),
                as_json,
            )
            raise SystemExit(error.exit_code) from exc
        except (KeyboardInterrupt, click.Abort) as exc:
            emit(
                Response(
                    status="error",
                    error=ErrorDetail(code="INTERRUPTED", message="Operation interrupted."),
                ),
                as_json,
            )
            raise SystemExit(7) from exc
        except Exception as exc:
            # Do not echo exception repr/tracebacks: providers may put tokens/track XML there.
            emit(
                Response(
                    status="error",
                    error=ErrorDetail(
                        code="INTERNAL_ERROR",
                        message="Unexpected local failure. Check installation and local state permissions.",
                    ),
                ),
                as_json,
            )
            raise SystemExit(7) from exc


app = typer.Typer(
    cls=JSONGroup,
    no_args_is_help=True,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
    help="Find existing outdoor routes, assess known closures, and prepare approved GPX handoffs.",
)


@app.callback()
def options(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None, "--config", help="TOML config path; [outdoor_agent] section."
    ),
    json_output: bool = typer.Option(False, "--json", help="One JSON response on stdout."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress optional diagnostic messages."),
    verbose: bool = typer.Option(
        False, "--verbose", help="Show non-sensitive diagnostics on stderr."
    ),
    version: bool = typer.Option(
        False, "--version", is_eager=True, help="Print application version."
    ),
) -> None:
    if quiet and verbose:
        raise OutdoorError("INVALID_ARGUMENTS", "--quiet and --verbose are mutually exclusive.")
    ctx.obj = {"config": config, "json": json_output, "quiet": quiet, "verbose": verbose}
    if version:
        if json_output or "--json" in sys.argv:
            emit(Response(status="ok", data={"version": __version__}), True)
        else:
            typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        raise OutdoorError("INVALID_ARGUMENTS", "Choose a command; see outdoor-agent --help.")


def settings(ctx: typer.Context) -> Settings:
    return load_settings(ctx.obj["config"])


def output(
    ctx: typer.Context,
    data: dict[str, Any],
    json_output: bool,
    warnings: list[str] | None = None,
    *,
    partial: bool = False,
    provider_status: list[ProviderStatus] | None = None,
    strict: bool = False,
) -> None:
    if ctx.obj["verbose"] and not ctx.obj["quiet"]:
        typer.echo("Command completed; sensitive provider details are omitted.", err=True)
    emit(
        Response(
            status="partial" if partial else "ok",
            data=data,
            warnings=warnings or [],
            provider_status=provider_status or [],
        ),
        json_output or ctx.obj["json"],
        quiet=ctx.obj["quiet"],
    )
    if partial and strict:
        raise SystemExit(6)


@app.command()
def providers(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Report capability evidence and adapter implementation independently."""
    config = settings(ctx)
    declarations = registry()
    for declaration in declarations:
        declaration.enabled = declaration.provider not in config.disabled_providers and (
            declaration.provider != "google" or config.google_enabled
        )
    output(
        ctx,
        {"providers": [provider.model_dump(mode="json") for provider in declarations]},
        json_output,
    )


@app.command()
def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
    probe: bool = typer.Option(
        False, "--probe", help="Perform a configured, read-only Ride with GPS auth probe."
    ),
) -> None:
    """Diagnose the local installation without network probes or credential values."""
    config = settings(ctx)
    ride_cli_installed = RideCLI(config).executable() is not None
    has_credentials = bool(config.ride_api_key and config.ride_auth_token)
    auth = {
        "state": "auth_required" if not has_credentials and not ride_cli_installed else "unverified"
    }
    if probe:
        if "ridewithgps" in config.disabled_providers:
            raise OutdoorError(
                "PROVIDER_DISABLED",
                "Ride with GPS is disabled in configuration.",
                5,
                provider="ridewithgps",
            )

        async def authenticate() -> dict[str, str]:
            async with HTTPClient(config) as http:
                return await RideWithGPS(config, http).probe_auth()

        auth = asyncio.run(authenticate())
    output(
        ctx,
        {
            "version": __version__,
            "python_version": sys.version.split()[0],
            "sqlite_version": sqlite3.sqlite_version,
            "offline": config.offline,
            "data_dir": str(config.data_dir),
            "cache_dir": str(config.cache_dir),
            "database_exists": (config.data_dir / "state.sqlite3").is_file(),
            "default_timezone": config.default_timezone,
            "default_region": config.default_region,
            "ride_cli_installed": ride_cli_installed,
            "ride_config_configured": bool(os.getenv("RIDE_CONFIG_DIR")),
            "ride_auth_state": auth["state"],
            "ride_auth": auth,
            "google_enabled": config.google_enabled,
            "google_key_configured": bool(os.getenv("GOOGLE_MAPS_API_KEY")),
            "external_probes_performed": probe,
            "implemented_commands": [
                "doctor",
                "providers",
                "inspect",
                "import-gpx",
                "schema",
                "search",
                "get",
                "routes",
                "check-closures",
                "recheck",
                "places",
                "export",
                "deliver",
            ],
            "provider_summary": [
                {"provider": p.provider, "implemented": p.implemented} for p in registry()
            ],
        },
        json_output,
        [
            "Provider coverage, authentication and export permissions vary; consult providers and the dated feasibility evidence."
        ],
    )


@app.command()
def inspect(
    ctx: typer.Context,
    path: Path,
    activity: Activity = typer.Option(Activity.other),
    source_url: str | None = typer.Option(None),
    source_type: SourceType = typer.Option(SourceType.unknown),
    attribution: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect a local GPX without writing cache, changing geometry, or calling providers."""
    settings(ctx)
    inspection = inspect_gpx(
        read_gpx(path),
        activity=activity,
        source_url=source_url,
        source_type=source_type,
        attribution=attribution,
    )
    output(ctx, inspection.model_dump(mode="json"), json_output, inspection.route.warnings)


@app.command("import-gpx")
def import_gpx(
    ctx: typer.Context,
    path: Path,
    activity: Activity = typer.Option(Activity.other),
    source_url: str | None = typer.Option(None),
    source_type: SourceType = typer.Option(SourceType.unknown),
    attribution: str | None = typer.Option(None),
    source_warning: list[str] = typer.Option(
        [],
        "--source-warning",
        help="Retain a source/access caveat with the saved route; repeat up to 20 times.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Store original GPX and inspection in private XDG state; repeated imports are idempotent."""
    config = settings(ctx)
    if len(source_warning) > 20 or any(
        not value.strip() or len(value) > 500 for value in source_warning
    ):
        raise OutdoorError(
            "INVALID_ARGUMENTS",
            "Source warnings must be nonempty, at most 500 characters each, with at most 20 values.",
        )
    content = read_gpx(path)
    inspection = inspect_gpx(
        content,
        activity=activity,
        source_url=source_url,
        source_type=source_type,
        attribution=attribution,
    )
    inspection.route.warnings.extend(value.strip() for value in source_warning)
    result = RouteStore(config.data_dir).import_route(inspection, content)
    warnings = list(result.inspection.route.warnings)
    if result.already_present:
        warnings.append("Identical GPX already imported; existing provenance retained.")
    output(ctx, result.model_dump(mode="json"), json_output, warnings)


class SchemaName(StrEnum):
    all = "all"
    response = "response"
    route = "route"
    inspection = "inspection"
    import_result = "import_result"
    provider = "provider"
    closure_event = "closure_event"
    closure_finding = "closure_finding"
    closure_report = "closure_report"
    discovery = "discovery"
    places = "places"
    export = "export"
    export_plan = "export_plan"
    handoff = "handoff"


def schemas() -> dict[str, dict[str, Any]]:
    models: dict[str, type[BaseModel]] = {
        "response": Response,
        "route": Route,
        "inspection": GPXInspection,
        "import_result": ImportResult,
        "provider": Provider,
        "closure_event": ClosureEvent,
        "closure_finding": ClosureFinding,
        "closure_report": ClosureReport,
        "discovery": DiscoveryResult,
        "places": PlacesResult,
        "export": ExportResult,
        "export_plan": ExportPlan,
        "handoff": HandoffResult,
    }
    return {
        name: {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **model.model_json_schema(),
        }
        for name, model in models.items()
    }


@app.command()
def schema(
    ctx: typer.Context,
    name: SchemaName = typer.Option(SchemaName.all),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Export versioned Pydantic JSON schemas to stdout inside the JSON envelope."""
    available = schemas()
    selected = available if name == SchemaName.all else {name.value: available[name.value]}
    output(ctx, {"schemas": selected}, json_output)


def parse_distance(value: str | None) -> tuple[float | None, float | None]:
    if value is None:
        return None, None
    import math

    try:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("Expected min:max")
        low, high = float(parts[0]), float(parts[1])
        if not math.isfinite(low) or not math.isfinite(high) or low < 0 or high < low:
            raise ValueError("Invalid range")
        return low * 1000, high * 1000
    except ValueError as exc:
        raise OutdoorError(
            "INVALID_DISTANCE_RANGE",
            "Use --distance-km MIN:MAX with finite nonnegative values and MIN <= MAX.",
        ) from exc


def selected_route(
    config: Settings, gpx: Path | None, route_id: str | None, activity: Activity | None = None
) -> Route:
    if (gpx is None) == (route_id is None):
        raise OutdoorError("INVALID_ARGUMENTS", "Choose exactly one of --gpx or --route-id.")
    if gpx is not None:
        return inspect_gpx(read_gpx(gpx), activity=activity or Activity.other).route
    assert route_id is not None
    return RouteStore(config.data_dir).get_route(route_id)


@app.command()
def search(
    ctx: typer.Context,
    near: str | None = typer.Option(
        None, help="Place name or latitude,longitude; optional with bbox."
    ),
    radius_km: float = typer.Option(30, min=0.1, max=100),
    activity: Activity = typer.Option(Activity.hiking),
    distance_km: str | None = typer.Option(None),
    loop: str = typer.Option("preferred"),
    shape: str = typer.Option("any", help="any, loop or point_to_point; known shape required."),
    bbox: str | None = typer.Option(None, help="Bounded WGS84 south,west,north,east query extent."),
    date: str | None = typer.Option(None, help="ISO trip date/time for closure-aware ranking."),
    feed_file: Path | None = typer.Option(None, help="Explicit closure fixture; requires --date."),
    limit: int = typer.Option(10, min=1, max=100),
    source_type: str = typer.Option(
        "all", help="all, official_trail or community; generated routes excluded."
    ),
    provider: list[str] = typer.Option(
        [], "--provider", help="Select providers; repeat to combine."
    ),
    strict: bool = typer.Option(False, help="Exit 6 for partial coverage after emitting results."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Discover real mapped OSM routes; disclose unavailable community catalogs."""
    config = settings(ctx)
    low, high = parse_distance(distance_km)
    bounds = None
    if bbox is not None:
        try:
            bounds = tuple(float(value) for value in bbox.split(","))
            if len(bounds) != 4:
                raise ValueError("Expected four coordinates")
        except ValueError as exc:
            raise OutdoorError("INVALID_BOUNDS", "Use --bbox SOUTH,WEST,NORTH,EAST.") from exc
    enabled = [name for name in (provider or ["osm"]) if name not in config.disabled_providers]
    if not enabled:
        raise OutdoorError(
            "PROVIDERS_DISABLED", "All selected discovery providers are disabled.", 5
        )
    try:
        criteria = SearchCriteria.model_validate(
            {
                "near": near,
                "radius_km": radius_km,
                "activity": activity,
                "distance_min_m": low,
                "distance_max_m": high,
                "loop": loop,
                "shape": shape,
                "bbox": bounds,
                "date": date,
                "feed_file": feed_file,
                "limit": limit,
                "source_type": source_type,
                "providers": enabled,
            }
        )
    except ValidationError as exc:
        raise OutdoorError(
            "INVALID_ARGUMENTS",
            "Invalid search options; use valid providers, source type and loop values.",
        ) from exc
    result = asyncio.run(search_routes(config, criteria))
    store = RouteStore(config.data_dir)
    for route in result.normalized_routes:
        store.save_route(route)
    statuses = [
        ProviderStatus(provider=item.provider, status=item.status, reason=item.reason)
        for item in result.provider_status
    ]
    data = result.model_dump(mode="json")
    data["total"] = len(result.routes)
    output(
        ctx,
        data,
        json_output,
        result.warnings,
        partial=result.partial,
        provider_status=statuses,
        strict=strict,
    )


@app.command("get")
def get_route(
    ctx: typer.Context,
    route_id: str = typer.Option(...),
    refresh: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read a saved route, or fetch supported OSM/authenticated RWGPS metadata."""
    config = settings(ctx)
    route = asyncio.run(load_route(config, route_id, refresh=refresh))
    output(
        ctx,
        {"route": route.model_dump(mode="json")},
        json_output,
        route.warnings,
        partial=route.geometry_quality in {"incomplete", "unknown"}
        or any(
            "stale" in warning.lower() or "partial response" in warning.lower()
            for warning in route.warnings
        ),
    )


@app.command("routes")
def list_routes(
    ctx: typer.Context,
    limit: int = typer.Option(100, min=1, max=1000),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List saved imports and discoveries without provider requests."""
    routes = RouteStore(settings(ctx).data_dir).list_routes(limit)
    summaries = [
        {
            "id": route.id,
            "name": route.name,
            "provider": route.provider,
            "source_url": route.source_url,
            "source_type": route.source_type.value,
            "attribution": route.attribution,
            "activity": route.activity.value,
            "distance_m": route.distance_m,
            "gpx_access": route.gpx_access,
        }
        for route in routes
    ]
    output(ctx, {"routes": summaries, "total": len(summaries)}, json_output)


def validate_trip_date(config: Settings, date: str) -> None:
    try:
        parse_window(date, config.default_timezone)
    except (ValueError, TypeError) as exc:
        raise OutdoorError(
            "INVALID_DATE",
            "Supply an ISO date or datetime; ambiguous/nonexistent local times need an explicit UTC offset.",
        ) from exc


def closure_result(
    config: Settings,
    route: Route,
    date: str,
    activity: Activity,
    feed_file: Path | None,
    feed_result: FeedFetchResult | None = None,
    osm_restrictions: bool = False,
) -> tuple[ClosureReport, dict[str, object]]:
    validate_trip_date(config, date)
    report = asyncio.run(
        assess_route(
            config,
            route,
            date,
            activity,
            feed_file=feed_file,
            feed_result=feed_result,
            osm_restrictions=osm_restrictions,
        )
    )
    value = report.model_dump(mode="json")
    evidence = {
        "status": report.status,
        "findings": sorted(
            [finding.model_dump(mode="json") for finding in report.findings],
            key=lambda item: str(item["event_id"]),
        ),
        "stale": report.stale,
        "source_status": report.source_status,
        "unknown_coverage": report.unknown_coverage,
        "horizon_status": report.horizon_status,
        "coverage_summaries": [
            summary.model_dump(mode="json") for summary in report.coverage_summaries
        ],
        "source_errors": sorted(
            [error.model_dump(mode="json") for error in report.source_errors],
            key=lambda item: (item["source"], item["code"]),
        ),
        "events": sorted(
            [
                event.model_dump(mode="json", exclude={"observed_at", "published_at"})
                for event in report.events
            ],
            key=lambda item: str(item["id"]),
        ),
    }
    request_key = f"{date}|{activity.value}|{'fixture:' + str(feed_file.absolute()) if feed_file else 'live'}|osm={osm_restrictions}"
    comparison = RouteStore(config.data_dir).record_check(route.id, request_key, value, evidence)
    return report, comparison


@app.command("check-closures")
def check_closures(
    ctx: typer.Context,
    gpx: Path | None = typer.Option(None),
    route_id: str | None = typer.Option(None),
    date: str = typer.Option(...),
    activity: Activity | None = typer.Option(None),
    osm_restrictions: bool = typer.Option(
        False,
        help="Also query bounded OSM access/construction tags; trail coverage remains incomplete.",
    ),
    feed_file: Path | None = typer.Option(
        None, help="Explicit local DATEX XML/gzip fixture; output marked fixture_mode."
    ),
    strict: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Assess current/planned NDW evidence; unknown coverage never means open."""
    config = settings(ctx)
    route = selected_route(config, gpx, route_id, activity)
    report, comparison = closure_result(
        config,
        route,
        date,
        activity or route.activity,
        feed_file,
        osm_restrictions=osm_restrictions,
    )
    partial = (
        report.status in {"coverage_unknown", "check_failed", "possible_conflict"}
        or report.source_status in {"partial", "failed"}
        or report.stale
        or report.unknown_coverage
    )
    output(
        ctx,
        {"assessment": report.model_dump(mode="json"), "comparison": comparison},
        json_output,
        report.warnings,
        partial=partial,
        strict=strict,
    )


@app.command("recheck")
def recheck(
    ctx: typer.Context,
    route_id: list[str] = typer.Option([], "--route-id"),
    date: str = typer.Option(...),
    activity: Activity | None = typer.Option(None),
    osm_restrictions: bool = typer.Option(
        False, help="Also query bounded OSM restriction evidence for each selected route."
    ),
    feed_file: Path | None = typer.Option(None),
    strict: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reassess selected saved routes and compare stable findings to the previous check."""
    config = settings(ctx)
    store = RouteStore(config.data_dir)
    routes = [store.get_route(value) for value in route_id] if route_id else store.list_routes()
    if not routes:
        raise OutdoorError("ROUTE_NOT_FOUND", "There are no saved routes to recheck.")
    validate_trip_date(config, date)
    feed_result = (
        asyncio.run(fetch_feeds(config))
        if feed_file is None and "ndw" not in config.disabled_providers
        else None
    )
    results = []
    warnings = []
    partial = False
    for route in routes:
        report, comparison = closure_result(
            config,
            route,
            date,
            activity or route.activity,
            feed_file,
            feed_result,
            osm_restrictions,
        )
        results.append({"assessment": report.model_dump(mode="json"), "comparison": comparison})
        warnings.extend(report.warnings)
        partial = (
            partial
            or report.status in {"coverage_unknown", "check_failed", "possible_conflict"}
            or report.source_status in {"partial", "failed"}
            or report.stale
            or report.unknown_coverage
        )
    output(
        ctx,
        {"checks": results, "total": len(results)},
        json_output,
        list(dict.fromkeys(warnings)),
        partial=partial,
        strict=strict,
    )


@app.command()
def places(
    ctx: typer.Context,
    gpx: Path | None = typer.Option(None),
    route_id: str | None = typer.Option(None),
    category: str = typer.Option("viewpoint"),
    provider: str = typer.Option("google"),
    strict: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Opt-in, budget-limited Google Places enrichment with attribution."""
    config = settings(ctx)
    route = selected_route(config, gpx, route_id)
    result = asyncio.run(enrich_places(config, route, category, provider))
    output(
        ctx,
        result.model_dump(mode="json"),
        json_output,
        result.warnings,
        partial=bool(result.warnings),
        strict=strict,
    )


@app.command()
def export(
    ctx: typer.Context,
    route_id: str = typer.Option(...),
    format: str = typer.Option("gpx"),
    output_path: Path = typer.Option(..., "--output"),
    overwrite: bool = typer.Option(False),
    dry_run: bool = typer.Option(False),
    confirm: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Default dry-run; --confirm exports permitted GPX to the reviewed local path."""
    if format != "gpx":
        raise OutdoorError(
            "EXPORT_FORMAT_UNSUPPORTED", "Only COROS-compatible GPX export is supported.", 5
        )
    if dry_run and confirm:
        raise OutdoorError("INVALID_ARGUMENTS", "Choose --dry-run or --confirm, not both.")
    config = settings(ctx)
    if not confirm:
        route = RouteStore(config.data_dir).get_route(route_id)
        plan = ExportPlan(
            route_id=route_id,
            path=str(output_path.absolute()),
            overwrite=overwrite,
            source_url=route.source_url,
            side_effects=[
                "Write a local GPX file and cache the permitted artifact",
                "Fetch provider GPX only if required and authorized",
            ],
            warnings=list(route.warnings),
        )
        output(ctx, plan.model_dump(mode="json"), json_output, plan.warnings)
        return
    result = asyncio.run(export_route(config, route_id, output_path, overwrite=overwrite))
    output(ctx, result.model_dump(mode="json"), json_output, result.warnings)


@app.command()
def deliver(
    ctx: typer.Context,
    route_id: str = typer.Option(...),
    target: str = typer.Option("coros"),
    dry_run: bool = typer.Option(False),
    confirm: bool = typer.Option(False),
    output_path: Path | None = typer.Option(None, "--output"),
    account: str | None = typer.Option(
        None, help="Human account label for the plan; no login or credentials."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Default dry-run; --confirm prepares a local GPX for manual COROS app import."""
    result = asyncio.run(
        prepare_handoff(
            settings(ctx),
            route_id,
            target=target,
            confirm=confirm,
            dry_run=dry_run,
            output=output_path,
            account=account,
        )
    )
    output(ctx, result.model_dump(mode="json"), json_output, result.warnings)


def main() -> None:
    app()
