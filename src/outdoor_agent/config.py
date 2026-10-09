"""Validated TOML/environment configuration with XDG defaults."""

import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator

from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Model


class Settings(Model):
    data_dir: Path
    cache_dir: Path
    default_timezone: str = "Europe/Amsterdam"
    default_region: str = "NL"
    http_timeout_seconds: float = Field(default=20, gt=0, le=120)
    offline: bool = False
    google_enabled: bool = False
    max_google_requests_per_run: int = Field(default=20, ge=0, le=1000)

    @field_validator("default_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @field_validator("data_dir", "cache_dir")
    @classmethod
    def absolute_dir(cls, value: Path) -> Path:
        return value.expanduser().absolute()


def load_settings(config_path: Path | None = None) -> Settings:
    """Precedence: explicit environment > TOML > XDG/defaults; no dotenv loading."""
    home = Path.home()
    default_config = (
        Path(os.getenv("XDG_CONFIG_HOME", str(home / ".config"))) / "outdoor-agent/config.toml"
    )
    path = config_path or default_config
    values: dict[str, object] = {
        "data_dir": Path(os.getenv("XDG_DATA_HOME", str(home / ".local/share"))) / "outdoor-agent",
        "cache_dir": Path(os.getenv("XDG_CACHE_HOME", str(home / ".cache"))) / "outdoor-agent",
    }
    if config_path is not None or path.exists():
        try:
            with path.open("rb") as file:
                document = tomllib.load(file)
            if set(document) - {"outdoor_agent"}:
                raise ValueError("Unknown config sections")
            section = document.get("outdoor_agent", {})
            if not isinstance(section, dict):
                raise ValueError("Invalid config section")
            values.update(section)
        except (OSError, ValueError) as exc:
            raise OutdoorError(
                "INVALID_CONFIG", "Cannot read valid TOML [outdoor_agent] configuration."
            ) from exc
    for field in Settings.model_fields:
        value = os.getenv(f"OUTDOOR_AGENT_{field.upper()}")
        if value:
            values[field] = value
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        # Validation errors can include secrets in input; expose field names only.
        fields = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
        raise OutdoorError(
            "INVALID_CONFIG", f"Invalid configuration fields: {', '.join(fields)}."
        ) from exc
