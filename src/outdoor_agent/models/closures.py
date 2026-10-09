"""Closure contracts for later adapters; no Phase 1 assessment claims."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from outdoor_agent.models.routes import Geometry, Model, now

Assessment = Literal[
    "known_conflict", "possible_conflict", "no_known_conflict", "coverage_unknown", "check_failed"
]


class ClosureEvent(Model):
    id: str
    provider: str
    source_url: str | None = None
    description: str
    issuer: str | None = None
    geometry: Geometry | None = None
    location_reference: str | None = None
    geometry_quality: Literal["precise", "approximate", "unresolved"] = "unresolved"
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    recurrence: str | None = None
    timezone: str | None = None
    affected_modes: list[Literal["road", "bike", "foot", "other"]] = Field(default_factory=list)
    restriction_type: str
    direction: str | None = None
    status: str
    observed_at: datetime = Field(default_factory=now)
    published_at: datetime | None = None
    expires_at: datetime | None = None
    raw_metadata_reference: str | None = None


class ClosureFinding(Model):
    route_id: str
    event_id: str
    classification: Assessment
    reason: str
    distance_along_route_m: float | None = Field(default=None, ge=0)
    min_distance_to_route_m: float | None = Field(default=None, ge=0)
    overlap_length_m: float | None = Field(default=None, ge=0)
    temporal_overlap: bool | None = None
    mode_overlap: bool | None = None
    geometry_confidence: Literal["precise", "approximate", "unknown"] = "unknown"
    source_url: str | None = None
    evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
