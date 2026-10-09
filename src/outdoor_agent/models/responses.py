"""Versioned envelopes used by every information command."""

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from outdoor_agent.models.routes import Model, now


class ErrorDetail(Model):
    code: str
    message: str
    provider: str | None = None
    retryable: bool = False
    suggestions: list[str] = Field(default_factory=list)


class ProviderStatus(Model):
    provider: str
    status: Literal["ok", "unsupported", "unverified", "restricted", "degraded", "disabled"]
    reason: str | None = None


class Response(Model):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["ok", "partial", "error"]
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    generated_at: datetime = Field(default_factory=now)
    data: dict[str, Any] | None = None
    error: ErrorDetail | None = None
    provider_status: list[ProviderStatus] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_status(self) -> "Response":
        if self.status == "error" and (self.error is None or self.data is not None):
            raise ValueError("Error responses require error and no data")
        if self.status != "error" and (self.data is None or self.error is not None):
            raise ValueError("Success/partial responses require data and no error")
        if self.status == "partial" and not (self.warnings or self.provider_status):
            raise ValueError("Partial responses must disclose limitations")
        return self
