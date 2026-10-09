"""OpenClaw-style CLI calls, including failure and redaction contracts."""

import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import validate
from typer.testing import CliRunner

from outdoor_agent import cli
from outdoor_agent.cli import app, schemas

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]


def invoke(arguments: list[str], code: int = 0) -> dict:
    result = runner.invoke(app, arguments)
    assert result.exit_code == code, (result.output, result.exception)
    # json.loads rejects additional JSON objects or progress output.
    payload = json.loads(result.stdout)
    validate(payload, schemas()["response"])
    return payload


@pytest.mark.parametrize("command", ["providers", "doctor", "schema"])
@pytest.mark.parametrize("global_json", [False, True])
def test_information_json(command: str, global_json: bool, tmp_path: Path) -> None:
    arguments = ["--json", command] if global_json else [command, "--json"]
    assert invoke(arguments)["status"] == "ok"
    assert not (tmp_path / "XDG_DATA_HOME").exists()


def test_inspect_contract(example_gpx: Path, tmp_path: Path) -> None:
    response = invoke(["inspect", str(example_gpx), "--activity", "gravel", "--json"])
    validate(response["data"], schemas()["inspection"])
    route = response["data"]["route"]
    assert route["source_type"] == "unknown"
    assert route["activity"] == "gravel"
    assert route["rating"] is None
    assert route["geometry"]["crs"] == "EPSG:4326"
    assert not (tmp_path / "XDG_DATA_HOME").exists()


def test_import_repeat_original_bytes_and_provenance(example_gpx: Path, tmp_path: Path) -> None:
    arguments = [
        "import-gpx",
        str(example_gpx),
        "--source-url",
        "https://ridewithgps.com/routes/12345",
        "--source-type",
        "community",
        "--attribution",
        "Synthetic author",
        "--json",
    ]
    first = invoke(arguments)
    second = invoke(arguments)
    validate(first["data"], schemas()["import_result"])
    assert first["data"]["already_present"] is False
    assert second["data"]["already_present"] is True
    assert first["data"]["inspection"] == second["data"]["inspection"]
    assert Path(first["data"]["artifact"]["path"]).read_bytes() == example_gpx.read_bytes()
    db_path = tmp_path / "XDG_DATA_HOME/outdoor-agent/state.sqlite3"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM routes").fetchone()[0] == 1


@pytest.mark.parametrize(
    "arguments,error_code",
    [
        (["inspect", "missing.gpx", "--json"], "GPX_NOT_FOUND"),
        (["inspect", "--json"], "INVALID_ARGUMENTS"),
        (["providers", "--bogus", "SECRET_SENTINEL", "--json"], "INVALID_ARGUMENTS"),
        (["--json", "unknown-command"], "INVALID_ARGUMENTS"),
        (["schema", "--name", "bad", "--json"], "INVALID_ARGUMENTS"),
        (["--quiet", "--verbose", "doctor", "--json"], "INVALID_ARGUMENTS"),
    ],
)
def test_errors_are_single_redacted_json(arguments: list[str], error_code: str) -> None:
    response = invoke(arguments, 2)
    assert response["error"]["code"] == error_code
    assert "SECRET_SENTINEL" not in json.dumps(response)


def test_malformed_input_is_redacted(tmp_path: Path) -> None:
    path = tmp_path / "bad.gpx"
    path.write_text("<gpx>SECRET_SENTINEL")
    assert "SECRET_SENTINEL" not in json.dumps(invoke(["inspect", str(path), "--json"], 2))


@pytest.mark.parametrize(
    "arguments,code,error",
    [
        (
            ["search", "--near", "Utrecht", "--source-type", "community"],
            5,
            "COMMUNITY_DISCOVERY_UNAVAILABLE",
        ),
        (["check-closures", "--gpx", "missing.gpx", "--date", "2026-10-11"], 2, "GPX_NOT_FOUND"),
        (["places", "--gpx", "missing.gpx", "--category", "viewpoint"], 2, "GPX_NOT_FOUND"),
        (["export", "--route-id", "ridewithgps:1", "--output", "unused.gpx"], 3, "AUTH_REQUIRED"),
        (["deliver", "--route-id", "ridewithgps:1", "--dry-run"], 2, "ROUTE_NOT_FOUND"),
        (["deliver", "--route-id", "ridewithgps:1", "--confirm"], 2, "ROUTE_NOT_FOUND"),
    ],
)
def test_unavailable_operations_fail_honestly(arguments: list[str], code: int, error: str) -> None:
    response = invoke([*arguments, "--json"], code)
    assert response["status"] == "error"
    assert response["error"]["code"] == error


def test_doctor_never_exposes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "SECRET_SENTINEL")
    monkeypatch.setenv("RIDE_CONFIG_DIR", "/SECRET_SENTINEL")
    response = invoke(["doctor", "--json"])
    assert "SECRET_SENTINEL" not in json.dumps(response)
    assert response["data"]["google_key_configured"] is True
    assert response["data"]["ride_auth_state"] == "auth_required"


def test_version() -> None:
    assert runner.invoke(app, ["--version"]).stdout.strip() == "0.2.0"
    assert invoke(["--json", "--version"])["data"]["version"] == "0.2.0"


def test_logs_go_to_stderr() -> None:
    result = runner.invoke(app, ["--verbose", "providers", "--json"])
    json.loads(result.stdout)
    assert "Command completed" in result.stderr


def test_unexpected_failure_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken():
        raise RuntimeError("SECRET_SENTINEL")

    monkeypatch.setattr(cli, "registry", broken)
    response = invoke(["providers", "--json"], 7)
    assert response["error"]["code"] == "INTERNAL_ERROR"
    assert "SECRET_SENTINEL" not in json.dumps(response)


def test_published_schemas_match_models() -> None:
    for name, schema in schemas().items():
        assert json.loads((ROOT / f"schemas/v1/{name}.json").read_text()) == schema


def test_partial_example_matches_contract() -> None:
    payload = json.loads((ROOT / "tests/fixtures/partial-response.json").read_text())
    validate(payload, schemas()["response"])
    assert payload["status"] == "partial"
    assert payload["warnings"]


def test_json_without_command_is_typed_error() -> None:
    assert invoke(["--json"], 2)["error"]["code"] == "INVALID_ARGUMENTS"


def test_quiet_suppresses_optional_human_warnings() -> None:
    result = runner.invoke(app, ["--quiet", "doctor"])
    assert result.exit_code == 0
    assert not result.stderr


def test_embedded_source_url_is_preserved(example_gpx: Path) -> None:
    response = invoke(["inspect", str(example_gpx), "--json"])
    assert response["data"]["route"]["source_url"] == "https://ridewithgps.com/routes/12345"


def test_capabilities_do_not_enable_unimplemented_adapters() -> None:
    response = invoke(["providers", "--json"])
    providers = {item["provider"]: item for item in response["data"]["providers"]}
    assert providers["local"]["implemented"] is True
    assert all(
        providers[name]["implemented"] for name in ("osm", "ndw", "ridewithgps", "google", "coros")
    )
    assert not providers["wikiloc"]["implemented"]
    assert not providers["google"]["enabled"]
    assert providers["ridewithgps"]["capabilities"]["discover_public"]["state"] == "unsupported"
    assert providers["ndw"]["capabilities"]["fetch_closures"]["evidence"] == [
        "https://opendata.ndw.nu/tijdelijke_verkeersmaatregelen_afsluitingen.xml.gz"
    ]
    assert providers["coros"]["capabilities"]["partner_route_push"]["state"] == "restricted"
    assert providers["coros"]["capabilities"]["mcp_gpx_import"]["state"] == "unsupported"
