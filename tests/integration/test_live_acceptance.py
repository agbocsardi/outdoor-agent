"""Bounded, read-only live acceptance over the public CLI JSON contract.

Opt in with ``OUTDOOR_AGENT_LIVE_TESTS=1``. This module only performs documented
reads and one explicitly confirmed local GPX export. It never writes to an
account, imports to a watch, or publishes a route.
"""

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from outdoor_agent.cli import app
from outdoor_agent.geo.gpx import inspect_gpx

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("OUTDOOR_AGENT_LIVE_TESTS") != "1",
        reason="Set OUTDOOR_AGENT_LIVE_TESTS=1 to opt into bounded live acceptance.",
    ),
]

# tests/conftest.py strips OUTDOOR_AGENT_* variables before each live test runs, so
# the opt-in route override must be captured at import (collection) time.
LIVE_ROUTE = (
    os.environ.get("OUTDOOR_AGENT_LIVE_RIDE_ROUTE") or "https://ridewithgps.com/routes/100"
).strip()

READ_ONLY_COMMANDS = frozenset({"doctor", "get", "export", "search", "check-closures"})
ACCOUNT_WRITE_COMMANDS = frozenset({"deliver", "pin", "publish", "save", "upload"})

_runner = CliRunner()
_invoked: list[str] = []


def _canonical_id(reference: str) -> str:
    number = reference.rstrip("/").rsplit("/", 1)[-1].removeprefix("ridewithgps:")
    return f"ridewithgps:{number}"


def _invoke(*args: str) -> tuple[int, dict]:
    """Run one command with ``--json`` and return its exit code and single JSON object."""
    _invoked.append(args[0])
    result = _runner.invoke(app, [*args, "--json"])
    assert result.stdout.strip(), f"no JSON on stdout for {args}: {result.output!r}"
    return result.exit_code, json.loads(result.stdout)


def _skip_unavailable(context: str, payload: dict) -> None:
    code = payload.get("error", {}).get("code", "UNKNOWN")
    pytest.skip(f"{context} is unavailable in this live run ({code}); treating it as an outage.")


@pytest.fixture(autouse=True)
def _no_account_writes() -> Iterator[None]:
    """Item 6: the whole module may only call read-only commands."""
    _invoked.clear()
    yield
    invoked = set(_invoked)
    extra = invoked - READ_ONLY_COMMANDS
    assert not extra, f"non-read-only command invoked: {sorted(extra)}"
    assert not (invoked & ACCOUNT_WRITE_COMMANDS)


def test_bounded_read_only_live_acceptance(tmp_path: Path) -> None:
    # 1. Auth: an authenticated state must arrive through the ride CLI transport.
    exit_code, probe = _invoke("doctor", "--probe")
    if exit_code != 0:
        _skip_unavailable("ride CLI authentication probe", probe)
    diagnostics = probe["data"]
    assert diagnostics["ride_cli_installed"] is True
    assert diagnostics["external_probes_performed"] is True
    assert diagnostics["ride_auth"]["state"] == "authenticated"
    assert diagnostics["ride_auth"]["transport"] == "ride_cli"

    # 2. Known-route fetch by URL: stable id, provenance and non-empty geometry.
    exit_code, fetched = _invoke("get", "--route-id", LIVE_ROUTE, "--refresh")
    if exit_code != 0:
        _skip_unavailable("Ride with GPS route metadata", fetched)
    route = fetched["data"]["route"]
    route_id = _canonical_id(LIVE_ROUTE)
    number = route_id.split(":", 1)[1]
    assert route["id"] == route_id
    assert route["provider"] == "ridewithgps"
    assert route["provider_route_id"] == number
    assert route["source_url"] == f"https://ridewithgps.com/routes/{number}"
    assert route["attribution"]
    assert "Ride with GPS" in route["attribution"]
    geometry = route["geometry"]
    assert geometry is not None
    assert geometry["type"] in {"LineString", "MultiLineString"}
    assert len(geometry["coordinates"]) >= 2
    assert route["geometry_quality"] == "complete"

    # 3. Original GPX export preserves the provider bytes and is idempotent.
    first_path = tmp_path / "original.gpx"
    exit_code, exported = _invoke(
        "export", "--route-id", route_id, "--confirm", "--output", str(first_path)
    )
    if exit_code != 0:
        _skip_unavailable("Ride with GPS original GPX export", exported)
    receipt = exported["data"]
    first_bytes = first_path.read_bytes()
    assert receipt["status"] == "exported"
    assert receipt["original_bytes"] is True
    assert receipt["route_id"] == route_id
    assert receipt["source_url"] == route["source_url"]
    assert receipt["attribution"] == route["attribution"]
    assert receipt["byte_count"] == len(first_bytes)
    assert receipt["sha256"] == hashlib.sha256(first_bytes).hexdigest()
    inspection = inspect_gpx(first_bytes)
    assert inspection.point_count >= 2
    assert inspection.route.source_url == route["source_url"]
    repeat_path = tmp_path / "repeated.gpx"
    exit_code, repeated = _invoke(
        "export", "--route-id", route_id, "--confirm", "--output", str(repeat_path)
    )
    assert exit_code == 0, repeated
    assert repeat_path.read_bytes() == first_bytes
    assert repeated["data"]["sha256"] == receipt["sha256"]
    assert repeated["data"]["byte_count"] == receipt["byte_count"]

    # 4. Saved discovery is account-scoped: real saved routes or a typed empty.
    #    The public CLI requires a bounded location, so use a box around the known
    #    route; the provider raises the typed empty before that filter is applied.
    start_lat = route["start_lat"] if route["start_lat"] is not None else 0.0
    start_lon = route["start_lon"] if route["start_lon"] is not None else 0.0
    bbox = (
        f"{start_lat - 0.2:.4f},{start_lon - 0.2:.4f},{start_lat + 0.2:.4f},{start_lon + 0.2:.4f}"
    )
    exit_code, discovered = _invoke("search", "--provider", "ridewithgps", "--bbox", bbox)
    if exit_code != 0:
        error = discovered["error"]
        assert error["code"] == "RIDEWITHGPS_NO_SAVED_ROUTES", error
        assert error["provider"] == "ridewithgps"
    else:
        found = discovered["data"]
        saved = found["routes"]
        assert found["total"] == len(saved) >= 1, (
            "saved discovery returned an empty success instead of the typed empty error"
        )
        assert any("authenticated account" in warning for warning in found["warnings"])
        for item in saved:
            saved_route = item["route"]
            assert saved_route["provider"] == "ridewithgps"
            assert saved_route["id"].startswith("ridewithgps:")
            assert saved_route["source_url"].startswith("https://ridewithgps.com/")
            assert saved_route["attribution"]
            assert saved_route["geometry"] is not None

    # 5. Dated closure assessment: only the typed contract plus explicit uncertainty.
    trip_date = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    exit_code, closures = _invoke("check-closures", "--route-id", route_id, "--date", trip_date)
    assert exit_code == 0, closures
    assessment = closures["data"]["assessment"]
    assert assessment["route_id"] == route_id
    assert assessment["requested_date"] == trip_date
    assert assessment["status"] in {
        "known_conflict",
        "possible_conflict",
        "no_known_conflict",
        "coverage_unknown",
        "check_failed",
    }
    assert assessment["source_status"] in {"ok", "partial", "failed", "fixture"}
    assert assessment["coverage"]
    assert isinstance(assessment["unknown_coverage"], bool)
    assert (
        assessment["source_freshness"]
        or assessment["source_errors"]
        or assessment["unknown_coverage"]
    ), "closure assessment must expose source freshness or explicit uncertainty"
