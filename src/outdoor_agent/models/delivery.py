"""Plans and receipts for explicitly approved local GPX preparation."""

from pydantic import Field

from outdoor_agent.models.routes import Model
from outdoor_agent.providers.base import DeliveryPlan


class ExportPlan(Model):
    status: str = "export_planned"
    route_id: str
    target: str = "local_gpx"
    account: None = None
    path: str
    dry_run: bool = True
    overwrite: bool = False
    source_url: str | None
    side_effects: list[str]
    warnings: list[str] = Field(default_factory=list)


class ExportResult(Model):
    status: str = "exported"
    route_id: str
    path: str
    sha256: str
    byte_count: int
    original_bytes: bool
    source_url: str | None
    attribution: str
    warnings: list[str] = Field(default_factory=list)


class HandoffResult(DeliveryPlan):
    target: str = "coros"
    account: str | None = None
    status: str
    dry_run: bool
    path: str
    source_url: str | None
    idempotency_key: str
    already_prepared: bool = False
    instructions: list[str]
    watch_status: str = "watch_sync_unverified"
    warnings: list[str] = Field(default_factory=list)
