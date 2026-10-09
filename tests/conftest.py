"""Every baseline test is offline and uses isolated local state."""

import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "OUTDOOR_AGENT_DATA_DIR",
        "OUTDOOR_AGENT_CACHE_DIR",
        "OUTDOOR_AGENT_DEFAULT_TIMEZONE",
        "OUTDOOR_AGENT_DEFAULT_REGION",
        "OUTDOOR_AGENT_HTTP_TIMEOUT_SECONDS",
        "OUTDOOR_AGENT_OFFLINE",
        "OUTDOOR_AGENT_GOOGLE_ENABLED",
        "OUTDOOR_AGENT_MAX_GOOGLE_REQUESTS_PER_RUN",
        "GOOGLE_MAPS_API_KEY",
        "RIDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("Baseline tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.fixture
def example_gpx() -> Path:
    return Path(__file__).parent / "fixtures/example.gpx"
