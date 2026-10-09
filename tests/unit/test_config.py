from pathlib import Path

import pytest

from outdoor_agent.config import load_settings
from outdoor_agent.errors import OutdoorError


def test_xdg_defaults_do_not_create_dirs(tmp_path: Path) -> None:
    settings = load_settings()
    assert settings.data_dir == tmp_path / "XDG_DATA_HOME/outdoor-agent"
    assert settings.cache_dir == tmp_path / "XDG_CACHE_HOME/outdoor-agent"
    assert settings.default_timezone == "Europe/Amsterdam"
    assert settings.google_enabled is False
    assert not settings.data_dir.exists()


def test_toml_and_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        '[outdoor_agent]\ndefault_region="DE"\noffline=true\nhttp_timeout_seconds=5\n'
    )
    monkeypatch.setenv("OUTDOOR_AGENT_DEFAULT_REGION", "NL")
    settings = load_settings(config)
    assert settings.default_region == "NL"
    assert settings.offline is True
    assert settings.http_timeout_seconds == 5


def test_default_toml_location(tmp_path: Path) -> None:
    directory = tmp_path / "XDG_CONFIG_HOME/outdoor-agent"
    directory.mkdir(parents=True)
    (directory / "config.toml").write_text('[outdoor_agent]\ndefault_timezone="UTC"\n')
    assert load_settings().default_timezone == "UTC"


@pytest.mark.parametrize(
    "value",
    [
        '[outdoor_agent]\nunknown="SECRET_SENTINEL"',
        '[outdoor_agent]\ndefault_timezone="SECRET_SENTINEL"',
        "[outdoor_agent]\nhttp_timeout_seconds=-1",
        '[outdoor_agent]\ngoogle_enabled="SECRET_SENTINEL"',
        "[wrong]\nvalue=1",
        "invalid!!",
        'outdoor_agent="SECRET_SENTINEL"',
        '[outdoor_agent]\noverpass_url="https://example.com/data?key=SECRET_SENTINEL"',
        '[outdoor_agent]\ndisabled_providers=["SECRET_SENTINEL"]',
        "[outdoor_agent]\nranking_weights={distance=-1}",
    ],
)
def test_invalid_config_is_redacted(tmp_path: Path, value: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(value)
    with pytest.raises(OutdoorError) as error:
        load_settings(config)
    assert error.value.code == "INVALID_CONFIG"
    assert "SECRET_SENTINEL" not in error.value.message


def test_explicit_missing_config_fails(tmp_path: Path) -> None:
    with pytest.raises(OutdoorError, match="TOML"):
        load_settings(tmp_path / "missing.toml")


def test_env_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTDOOR_AGENT_MAX_GOOGLE_REQUESTS_PER_RUN", "-1")
    with pytest.raises(OutdoorError):
        load_settings()
