"""Validated TOML/environment configuration with XDG defaults."""

import os
import tomllib
from pathlib import Path
from urllib.parse import urlsplit
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
    http_user_agent: str = "outdoor-agent/0.2.0 (+https://github.com/agbocsardi/outdoor-agent)"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    ndw_closures_url: str = (
        "https://opendata.ndw.nu/tijdelijke_verkeersmaatregelen_afsluitingen.xml.gz"
    )
    ndw_planned_url: str = (
        "https://opendata.ndw.nu/planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz"
    )
    ndw_cache_ttl_seconds: int = Field(default=600, ge=0, le=86400)
    ndw_max_age_seconds: int = Field(default=3600, ge=60, le=604800)
    closure_buffer_m: float = Field(default=20, ge=0, le=200)
    discovery_cache_ttl_seconds: int = Field(default=86400, ge=0, le=604800)
    places_cache_ttl_seconds: int = Field(default=0, ge=0, le=86400)
    ride_api_key: str | None = Field(default=None, exclude=True, repr=False)
    ride_auth_token: str | None = Field(default=None, exclude=True, repr=False)
    ride_cli_path: str | None = None
    ranking_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "distance": 1.0,
            "proximity": 1.0,
            "activity": 1.0,
            "loop": 1.0,
            "popularity": 1.0,
            "freshness": 1.0,
        }
    )
    disabled_providers: list[str] = Field(default_factory=list)
    offline: bool = False
    google_enabled: bool = False
    max_google_requests_per_run: int = Field(default=20, ge=0, le=1000)

    @field_validator("overpass_url", "nominatim_url", "ndw_closures_url", "ndw_planned_url")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        from outdoor_agent.providers.http import validate_endpoint

        try:
            validate_endpoint(value)
            if urlsplit(value).query:
                raise OutdoorError(
                    "UNSAFE_URL", "Configured endpoints cannot include query parameters."
                )
        except OutdoorError as exc:
            raise ValueError("Unsafe provider endpoint") from exc
        return value

    @field_validator("disabled_providers")
    @classmethod
    def known_disabled_providers(cls, value: list[str]) -> list[str]:
        from outdoor_agent.providers.registry import registry

        known = {provider.provider for provider in registry()} | {"alltrails", "strava"}
        if set(value) - known or len(set(value)) != len(value):
            raise ValueError("Disabled providers must be unique known provider names")
        return value

    @field_validator("ranking_weights")
    @classmethod
    def valid_weights(cls, value: dict[str, float]) -> dict[str, float]:
        import math

        allowed = {
            "distance",
            "proximity",
            "activity",
            "loop",
            "popularity",
            "freshness",
            "closures",
            "scenic",
        }
        if set(value) - allowed or any(not math.isfinite(v) or v < 0 for v in value.values()):
            raise ValueError("Ranking weights must be finite, nonnegative and known")
        return value

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
    for field, variable in (
        ("ride_api_key", "RIDEWITHGPS_API_KEY"),
        ("ride_auth_token", "RIDEWITHGPS_AUTH_TOKEN"),
    ):
        if os.getenv(variable):
            values[field] = os.getenv(variable)
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        # Validation errors can include secrets in input; expose field names only.
        fields = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
        raise OutdoorError(
            "INVALID_CONFIG", f"Invalid configuration fields: {', '.join(fields)}."
        ) from exc
