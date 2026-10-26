"""Every baseline test is offline and uses isolated local state."""

import os
import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    live_enabled = os.environ.get("OUTDOOR_AGENT_LIVE_TESTS") == "1"
    for name in list(os.environ):
        if name.startswith("OUTDOOR_AGENT_") or name in {
            "GOOGLE_MAPS_API_KEY",
            "RIDE_CONFIG_DIR",
            "RIDEWITHGPS_API_KEY",
            "RIDEWITHGPS_AUTH_TOKEN",
        }:
            monkeypatch.delenv(name, raising=False)
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    if live_enabled and request.node.get_closest_marker("live"):
        return

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("Baseline tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    # No baseline test may spawn the real ride CLI; tests inject a fake via ride_cli_path.
    monkeypatch.setattr("outdoor_agent.providers.ride_cli.which", lambda name: None)


@pytest.fixture
def example_gpx() -> Path:
    return Path(__file__).parent / "fixtures/example.gpx"
