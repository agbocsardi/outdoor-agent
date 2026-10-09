"""JSON-first CLI. Phase 1 performs only local operations."""

import json
import os
import shutil
import sqlite3
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

import click
import typer
from pydantic import BaseModel
from typer.core import TyperGroup

from outdoor_agent import __version__
from outdoor_agent.config import Settings, load_settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import inspect_gpx, read_gpx
from outdoor_agent.models.closures import ClosureEvent, ClosureFinding
from outdoor_agent.models.responses import ErrorDetail, Response
from outdoor_agent.models.routes import Activity, GPXInspection, ImportResult, Route, SourceType
from outdoor_agent.providers.registry import Provider, registry
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
    help="Inspect/import GPX locally and report evidence-based provider capabilities.",
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
    ctx: typer.Context, data: dict[str, Any], json_output: bool, warnings: list[str] | None = None
) -> None:
    if ctx.obj["verbose"] and not ctx.obj["quiet"]:
        typer.echo("Local operation; no provider requests made.", err=True)
    emit(
        Response(status="ok", data=data, warnings=warnings or []),
        json_output or ctx.obj["json"],
        quiet=ctx.obj["quiet"],
    )


@app.command()
def providers(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Report capability evidence and adapter implementation independently."""
    settings(ctx)
    output(
        ctx,
        {"providers": [provider.model_dump(mode="json") for provider in registry()]},
        json_output,
    )


@app.command()
def doctor(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Diagnose the local installation without network probes or credential values."""
    config = settings(ctx)
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
            "ride_cli_installed": shutil.which("ride") is not None,
            "ride_config_configured": bool(os.getenv("RIDE_CONFIG_DIR")),
            "ride_auth_state": "unverified",
            "google_enabled": config.google_enabled,
            "google_key_configured": bool(os.getenv("GOOGLE_MAPS_API_KEY")),
            "external_probes_performed": False,
            "implemented_commands": ["doctor", "providers", "inspect", "import-gpx", "schema"],
            "provider_summary": [
                {"provider": p.provider, "implemented": p.implemented} for p in registry()
            ],
        },
        json_output,
        [
            "External authentication/entitlements have not been checked; live adapters are later phases."
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
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Store original GPX and inspection in private XDG state; repeated imports are idempotent."""
    config = settings(ctx)
    content = read_gpx(path)
    inspection = inspect_gpx(
        content,
        activity=activity,
        source_url=source_url,
        source_type=source_type,
        attribution=attribution,
    )
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


def schemas() -> dict[str, dict[str, Any]]:
    models: dict[str, type[BaseModel]] = {
        "response": Response,
        "route": Route,
        "inspection": GPXInspection,
        "import_result": ImportResult,
        "provider": Provider,
        "closure_event": ClosureEvent,
        "closure_finding": ClosureFinding,
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


def unavailable(ctx: typer.Context, command: str, phase: int) -> None:
    settings(ctx)
    raise OutdoorError(
        "CAPABILITY_NOT_IMPLEMENTED",
        f"{command} is scheduled for Phase {phase}; no provider call or account write was made.",
        5,
        suggestions=[
            "Run outdoor-agent providers --json for capability evidence.",
            "Use inspect/import-gpx for a GPX obtained through an authorized provider export.",
        ],
    )


@app.command()
def search(
    ctx: typer.Context,
    near: str = typer.Option(...),
    radius_km: float = typer.Option(30, min=0.1, max=100),
    activity: Activity = typer.Option(Activity.other),
    distance_km: str | None = typer.Option(None),
    loop: str = typer.Option("preferred"),
    limit: int = typer.Option(10, min=1, max=100),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Phase 2 target; fails explicitly instead of returning fabricated discoveries."""
    unavailable(ctx, "search", 2)


@app.command("check-closures")
def check_closures(
    ctx: typer.Context,
    gpx: Path = typer.Option(...),
    date: str = typer.Option(...),
    activity: Activity = typer.Option(Activity.other),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Phase 3 target; Phase 1 cannot establish closure coverage."""
    unavailable(ctx, "check-closures", 3)


@app.command()
def places(
    ctx: typer.Context,
    gpx: Path = typer.Option(...),
    category: str = typer.Option(...),
    provider: str = typer.Option("google"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Phase 4 target; no paid Google calls made."""
    unavailable(ctx, "places", 4)


@app.command()
def export(
    ctx: typer.Context,
    route_id: str = typer.Option(...),
    format: str = typer.Option("gpx"),
    output: Path = typer.Option(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Phase 4 provider export target; imported original GPX is already stored locally."""
    unavailable(ctx, "export", 4)


@app.command()
def deliver(
    ctx: typer.Context,
    route_id: str = typer.Option(...),
    target: str = typer.Option("coros"),
    dry_run: bool = typer.Option(False),
    confirm: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Phase 4 target; neither confirmation nor dry-run enables an unverified API."""
    if dry_run and confirm:
        raise OutdoorError("INVALID_ARGUMENTS", "Choose --dry-run or --confirm, not both.")
    unavailable(ctx, "deliver", 4)


def main() -> None:
    app()
