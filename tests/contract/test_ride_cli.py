"""Offline contracts for the authenticated ``ride`` CLI transport (no real login, no network)."""

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from outdoor_agent.cli import app
from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import MAX_BYTES
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.ride_cli import RideCLI
from outdoor_agent.providers.ridewithgps import RideWithGPS

_FAKE_RIDE = """#!__PYTHON__
import json
import os
import sys
import time

argv_file = os.environ.get("FAKE_RIDE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as handle:
        json.dump(sys.argv, handle)

sleep = os.environ.get("FAKE_RIDE_SLEEP")
if sleep:
    time.sleep(float(sleep))

size = os.environ.get("FAKE_RIDE_STDOUT_BYTES")
if size:
    sys.stdout.buffer.write(b"x" * int(size))

stdout = os.environ.get("FAKE_RIDE_STDOUT")
if stdout:
    sys.stdout.buffer.write(stdout.encode("utf-8"))

stdout_file = os.environ.get("FAKE_RIDE_STDOUT_FILE")
if stdout_file:
    with open(stdout_file, "rb") as handle:
        sys.stdout.buffer.write(handle.read())

stderr = os.environ.get("FAKE_RIDE_STDERR")
if stderr:
    sys.stderr.buffer.write(stderr.encode("utf-8"))

sys.stdout.flush()
sys.stderr.flush()
sys.exit(int(os.environ.get("FAKE_RIDE_EXIT", "0")))
"""

_ROUTE_PAYLOAD: dict[str, Any] = {
    "route": {
        "id": 42,
        "user_id": 17,
        "name": "CLI transport route",
        "distance": 2000,
        "elevation_gain": 15,
        "first_lat": 52,
        "first_lng": 5,
        "last_lat": 52.01,
        "last_lng": 5.01,
        "activity_types": ["cycling:gravel"],
        "track_points": [{"x": 5, "y": 52}, {"x": 5.01, "y": 52.01}],
    }
}


@pytest.fixture
def fake_ride(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    script = tmp_path / "ride"
    script.write_text(_FAKE_RIDE.replace("__PYTHON__", sys.executable))
    script.chmod(0o755)
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_RIDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_RIDE_EXIT", "0")
    return script, argv_file


def cli_settings(tmp_path: Path, script: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "cache_dir": tmp_path / "cache",
        "ride_cli_path": str(script),
    }
    values.update(overrides)
    return Settings(**values)


def captured_argv(argv_file: Path) -> list[str]:
    return json.loads(argv_file.read_text())


async def test_argv_is_passed_without_a_shell(
    fake_ride: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, argv_file = fake_ride
    monkeypatch.setenv("FAKE_RIDE_STDOUT", '{"ok": true}')
    result = await RideCLI(cli_settings(tmp_path, script)).fetch(
        "/api/v1/routes/42.json", query={"filter": "a b; echo pwned"}, max_bytes=1024
    )
    assert json.loads(result.content) == {"ok": True}
    assert result.from_cache is False
    assert result.fetched_at.tzinfo is not None
    argv = captured_argv(argv_file)
    assert argv[1:4] == ["api", "get", "/api/v1/routes/42.json"]
    assert "filter=a b; echo pwned" in argv
    # A shell would have split the value; argv keeps it as exactly one element.
    assert "echo" not in argv
    assert "pwned" not in argv


async def test_raw_byte_success(
    fake_ride: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    example_gpx: Path,
) -> None:
    script, _ = fake_ride
    monkeypatch.setenv("FAKE_RIDE_STDOUT_FILE", str(example_gpx))
    result = await RideCLI(cli_settings(tmp_path, script)).fetch(
        "/api/v1/routes/42.gpx", max_bytes=MAX_BYTES
    )
    assert result.content == example_gpx.read_bytes()


@pytest.mark.parametrize(
    ("stderr", "code", "exit_code", "retryable"),
    [
        ("The requested resource cannot be found", "PROVIDER_NOT_FOUND", 4, False),
        ("Record not found", "PROVIDER_NOT_FOUND", 4, False),
        ("You are not logged in", "AUTH_REQUIRED", 3, False),
        ("please authenticate", "AUTH_REQUIRED", 3, False),
        ("unauthorized", "AUTH_EXPIRED", 3, False),
        ("HTTP 401", "AUTH_EXPIRED", 3, False),
        ("token expired", "AUTH_EXPIRED", 3, False),
        ("forbidden", "PERMISSION_DENIED", 3, False),
        ("403 permission denied", "PERMISSION_DENIED", 3, False),
        ("unexpected boom", "PROVIDER_UNAVAILABLE", 4, True),
    ],
)
async def test_error_mapping_is_redacted(
    fake_ride: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    code: str,
    exit_code: int,
    retryable: bool,
) -> None:
    script, _ = fake_ride
    monkeypatch.setenv("FAKE_RIDE_STDERR", f"{stderr} SECRET_SENTINEL")
    monkeypatch.setenv("FAKE_RIDE_EXIT", "1")
    with pytest.raises(OutdoorError) as error:
        await RideCLI(cli_settings(tmp_path, script)).fetch(
            "/api/v1/routes/42.json", max_bytes=1024
        )
    assert error.value.code == code
    assert error.value.exit_code == exit_code
    assert error.value.retryable is retryable
    assert error.value.provider == "ridewithgps"
    assert "SECRET_SENTINEL" not in str(error.value)
    assert "SECRET_SENTINEL" not in error.value.message


async def test_timeout_is_reaped_and_retryable(
    fake_ride: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = fake_ride
    monkeypatch.setenv("FAKE_RIDE_SLEEP", "30")
    with pytest.raises(OutdoorError) as error:
        await RideCLI(cli_settings(tmp_path, script, http_timeout_seconds=0.2)).fetch(
            "/api/v1/routes/42.json", max_bytes=1024
        )
    assert error.value.code == "PROVIDER_TIMEOUT"
    assert error.value.exit_code == 4
    assert error.value.retryable is True


async def test_output_overflow_is_bounded(
    fake_ride: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = fake_ride
    monkeypatch.setenv("FAKE_RIDE_STDOUT_BYTES", "4096")
    with pytest.raises(OutdoorError) as error:
        await RideCLI(cli_settings(tmp_path, script)).fetch("/api/v1/routes/42.json", max_bytes=64)
    assert error.value.code == "PROVIDER_PAYLOAD_TOO_LARGE"
    assert error.value.exit_code == 4


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/x",
        "/api/v1/../etc",
        "api/v1/x",
        "/api/v1/",
        "/api/v1/..",
        "https://evil.example/x",
        "/api/v1/x?y=1",
        "/api/v1/x\n",
    ],
)
async def test_invalid_path_rejected_before_spawn(
    fake_ride: tuple[Path, Path], tmp_path: Path, path: str
) -> None:
    script, argv_file = fake_ride
    with pytest.raises(OutdoorError) as error:
        await RideCLI(cli_settings(tmp_path, script)).fetch(path, max_bytes=1024)
    assert error.value.code == "INVALID_ARGUMENTS"
    assert error.value.exit_code == 2
    assert not argv_file.exists()


@pytest.mark.parametrize(
    "query", [{"a=b": "c"}, {"": "c"}, {"a": "b\x00c"}, {"a": "b\nc"}, {"a": "b\x7fc"}]
)
async def test_invalid_query_rejected_before_spawn(
    fake_ride: tuple[Path, Path], tmp_path: Path, query: dict[str, str]
) -> None:
    script, argv_file = fake_ride
    with pytest.raises(OutdoorError) as error:
        await RideCLI(cli_settings(tmp_path, script)).fetch(
            "/api/v1/routes/42.json", query=query, max_bytes=1024
        )
    assert error.value.code == "INVALID_ARGUMENTS"
    assert not argv_file.exists()


async def test_missing_executable_requires_login(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", ride_cli_path=None
    )
    cli = RideCLI(settings)
    assert cli.executable() is None
    with pytest.raises(OutdoorError) as error:
        await cli.fetch("/api/v1/users/current", max_bytes=1024)
    assert error.value.code == "AUTH_REQUIRED"
    assert error.value.exit_code == 3
    assert any("ride login" in suggestion for suggestion in error.value.suggestions)


async def test_empty_ride_cli_path_falls_back_to_which(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", ride_cli_path="")
    assert RideCLI(settings).executable() is None


async def test_get_uses_cli_transport(
    fake_ride: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, argv_file = fake_ride
    monkeypatch.setenv("FAKE_RIDE_STDOUT", json.dumps(_ROUTE_PAYLOAD))
    settings = cli_settings(tmp_path, script)
    async with HTTPClient(settings) as http:
        route = await RideWithGPS(settings, http).get("ridewithgps:42")
    assert route.provider_route_id == "42"
    assert route.name == "CLI transport route"
    assert route.attribution == "Ride with GPS route creator #17"
    assert captured_argv(argv_file)[1:4] == ["api", "get", "/api/v1/routes/42.json"]


async def test_export_bytes_uses_cli_transport(
    fake_ride: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    example_gpx: Path,
) -> None:
    script, argv_file = fake_ride
    monkeypatch.setenv("FAKE_RIDE_STDOUT_FILE", str(example_gpx))
    settings = cli_settings(tmp_path, script)
    async with HTTPClient(settings) as http:
        content = await RideWithGPS(settings, http).export_bytes("42")
    assert content == example_gpx.read_bytes()
    assert captured_argv(argv_file)[1:4] == ["api", "get", "/api/v1/routes/42.gpx"]


async def test_probe_auth_uses_cli_transport(
    fake_ride: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, argv_file = fake_ride
    monkeypatch.setenv(
        "FAKE_RIDE_STDOUT", json.dumps({"user": {"id": 17, "email": "private@example.com"}})
    )
    settings = cli_settings(tmp_path, script)
    async with HTTPClient(settings) as http:
        result = await RideWithGPS(settings, http).probe_auth()
    assert result["state"] == "authenticated"
    assert result["transport"] == "ride_cli"
    assert result["account_reference"] != "17"
    assert "private@example.com" not in json.dumps(result)
    assert captured_argv(argv_file)[1:4] == ["api", "get", "/api/v1/users/current"]


async def test_probe_auth_reports_http_transport(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        ride_api_key="synthetic-key",
        ride_auth_token="synthetic-token",
    )
    async with HTTPClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"user": {"id": 17}})
        ),
    ) as http:
        result = await RideWithGPS(settings, http).probe_auth()
    assert result["transport"] == "http"


def test_public_route_url_cli_chain_saves_searches_and_exports_original_bytes(
    fake_ride: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    example_gpx: Path,
) -> None:
    script, _ = fake_ride
    monkeypatch.setenv("OUTDOOR_AGENT_RIDE_CLI_PATH", str(script))
    runner = CliRunner()
    monkeypatch.setenv("FAKE_RIDE_STDOUT", json.dumps(_ROUTE_PAYLOAD))
    fetched = runner.invoke(
        app, ["get", "--route-id", "https://ridewithgps.com/routes/42", "--json"]
    )
    assert fetched.exit_code == 0, (fetched.output, fetched.exception)
    assert json.loads(fetched.stdout)["data"]["route"]["id"] == "ridewithgps:42"

    listed = runner.invoke(
        app,
        [
            "search",
            "--provider",
            "local",
            "--source-type",
            "community",
            "--bbox",
            "51.9,4.9,52.1,5.1",
            "--activity",
            "gravel",
            "--json",
        ],
    )
    assert listed.exit_code == 0, (listed.output, listed.exception)
    payload = json.loads(listed.stdout)
    assert [entry["route"]["id"] for entry in payload["data"]["routes"]] == ["ridewithgps:42"]

    monkeypatch.delenv("FAKE_RIDE_STDOUT")
    monkeypatch.setenv("FAKE_RIDE_STDOUT_FILE", str(example_gpx))
    output = tmp_path / "exported.gpx"
    exported = runner.invoke(
        app,
        [
            "export",
            "--confirm",
            "--route-id",
            "https://ridewithgps.com/routes/42",
            "--format",
            "gpx",
            "--output",
            str(output),
            "--json",
        ],
    )
    assert exported.exit_code == 0, (exported.output, exported.exception)
    body = json.loads(exported.stdout)["data"]
    assert body["original_bytes"] is True
    assert output.read_bytes() == example_gpx.read_bytes()
    assert body["sha256"] == hashlib.sha256(example_gpx.read_bytes()).hexdigest()


def test_doctor_reports_ride_cli_transport_without_credentials(
    fake_ride: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = fake_ride
    monkeypatch.setenv("OUTDOOR_AGENT_RIDE_CLI_PATH", str(script))
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, (result.output, result.exception)
    payload = json.loads(result.stdout)
    assert payload["data"]["ride_cli_installed"] is True
    assert payload["data"]["ride_auth_state"] == "unverified"
