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


DEN_BOSCH_CENTER = (5.3037, 51.6978)  # lon, lat
DEN_BOSCH_NEAR = "51.6978,5.3037"
_TRANSIENT_SEARCH_CODES = frozenset(
    {"PROVIDER_UNAVAILABLE", "PROVIDER_TIMEOUT", "PROVIDERS_UNAVAILABLE", "PROVIDER_RATE_LIMITED"}
)


def test_den_bosch_gravel_loop_public_explore(tmp_path: Path) -> None:
    """One bounded live proof of the experimental public Explore path.

    Finds a 20-35 km gravel loop within 20 km of Den Bosch through the undocumented
    public provider, then inspects one candidate through the permitted authenticated
    detail/export/closure path. It distinguishes suitability, geometry, closure and
    export uncertainty and never claims a ride-ready route.
    """
    from outdoor_agent.geo.geometry import distance_m

    exit_code, found = _invoke(
        "search",
        "--provider",
        "ridewithgps_public",
        "--source-type",
        "community",
        "--near",
        DEN_BOSCH_NEAR,
        "--radius-km",
        "20",
        "--activity",
        "gravel",
        "--distance-km",
        "20:35",
        "--shape",
        "loop",
        "--limit",
        "5",
    )
    if exit_code != 0:
        code = found.get("error", {}).get("code", "UNKNOWN")
        if code in _TRANSIENT_SEARCH_CODES:
            _skip_unavailable("experimental public Explore search", found)
        raise AssertionError(f"public Explore search failed: {found.get('error')}")
    assert found["status"] in {"ok", "partial"}
    statuses = {status["provider"]: status for status in found["data"]["provider_status"]}
    assert "ridewithgps_public" in statuses
    routes = found["data"]["routes"]
    assert routes, f"no public Den Bosch gravel loop returned: {found.get('warnings')}"
    assert any("experimental" in warning for warning in found["warnings"])

    candidate = routes[0]["route"]
    assert candidate["provider"] == "ridewithgps"
    assert candidate["id"].startswith("ridewithgps:")
    assert candidate["provider_route_id"] == candidate["id"].split(":", 1)[1]
    assert (
        candidate["source_url"]
        == f"https://ridewithgps.com/routes/{candidate['provider_route_id']}"
    )
    assert candidate["source_type"] == "community"
    assert candidate["attribution"] and "Ride with GPS" in candidate["attribution"]
    assert candidate["activity"] == "gravel"
    assert candidate["is_loop"] is True
    assert candidate["distance_m"] is not None and 20000 <= candidate["distance_m"] <= 35000
    start = (candidate["start_lon"], candidate["start_lat"])
    assert distance_m(DEN_BOSCH_CENTER, start) <= 20_000

    # Suitability is only a source label; the Explore record has no navigation geometry.
    assert candidate["geometry"] is None
    assert candidate["geometry_quality"] == "unknown"
    assert any("navigation geometry" in warning for warning in candidate["warnings"])

    route_id = candidate["id"]
    exit_code, detail = _invoke("get", "--route-id", route_id, "--refresh")
    if exit_code != 0:
        _skip_unavailable("permitted Ride with GPS route detail", detail)
    full = detail["data"]["route"]
    assert full["provider"] == "ridewithgps"
    assert full["geometry"] is not None
    assert full["geometry_quality"] == "complete"
    assert full["source_url"] == candidate["source_url"]
    # The Explore record attributes by byline name; the documented API detail
    # attributes by creator id, so only assert both are present and honest here.
    assert full["attribution"] and "Ride with GPS" in full["attribution"]

    first = tmp_path / "explore-original.gpx"
    exit_code, exported = _invoke(
        "export", "--route-id", route_id, "--confirm", "--output", str(first)
    )
    if exit_code != 0:
        _skip_unavailable("permitted original GPX export", exported)
    receipt = exported["data"]
    data = first.read_bytes()
    assert receipt["original_bytes"] is True
    assert receipt["byte_count"] == len(data)
    assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
    assert inspect_gpx(data).point_count >= 2
    repeated_path = tmp_path / "explore-original-again.gpx"
    exit_code, repeated = _invoke(
        "export", "--route-id", route_id, "--confirm", "--output", str(repeated_path)
    )
    assert exit_code == 0, repeated
    assert repeated_path.read_bytes() == data

    trip_date = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    exit_code, closures = _invoke("check-closures", "--route-id", route_id, "--date", trip_date)
    assert exit_code == 0, closures
    assessment = closures["data"]["assessment"]
    assert assessment["status"] in {
        "known_conflict",
        "possible_conflict",
        "no_known_conflict",
        "coverage_unknown",
        "check_failed",
    }
    assert assessment["source_status"] in {"ok", "partial", "failed", "fixture"}
    assert assessment["coverage"]
    assert (
        assessment["source_freshness"]
        or assessment["source_errors"]
        or assessment["unknown_coverage"]
    ), "closure assessment must expose freshness or explicit uncertainty"
    # A roadwork-free result must come from sources that were actually checked; it is
    # still not a guarantee of trail clearance.
    if assessment["status"] == "no_known_conflict":
        assert assessment["source_status"] == "ok"
        assert assessment["unknown_coverage"] is False
