"""Versioned event, finding and evidence-aware closure report contracts."""

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
    geometry_quality: Literal["precise", "approximate", "incomplete", "unresolved"] = "unresolved"
    severity: str | None = None
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
    # Additive fields: old serialized events remain valid.
    timing_quality: Literal["precise", "approximate", "unknown"] = "unknown"
    location_reference_type: str | None = None
    restriction_evidence: list[str] = Field(default_factory=list)
    mode_evidence: Literal["explicit", "scope", "unknown"] = "unknown"
    grade_separation_evidence: list[str] = Field(default_factory=list)


class ClosureFinding(Model):
    route_id: str
    event_id: str
    severity: str | None = None
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


class ClosureSourceError(Model):
    source: str
    code: str
    message: str


class ClosureSourceFreshness(Model):
    """Per-source refresh and feed publication provenance."""

    source: str
    last_successful_refresh: datetime | None = None
    publication_at: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    from_cache: bool = False
    stale: bool = False
    refresh_age_seconds: float | None = Field(default=None, ge=0)
    publication_age_seconds: float | None = Field(default=None, ge=0)


class ClosureReport(Model):
    """Conservative route assessment plus the feed provenance needed to interpret it."""

    route_id: str
    activity: str
    requested_date: str
    request_precision: Literal["date", "instant"] = "date"
    request_timezone: str = "Europe/Amsterdam"
    window_start: datetime | None = None
    window_end: datetime | None = None
    status: Assessment
    source_status: Literal["ok", "partial", "failed", "fixture"]
    findings: list[ClosureFinding] = Field(default_factory=list)
    events: list[ClosureEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    source_errors: list[ClosureSourceError] = Field(default_factory=list)
    source_freshness: list[ClosureSourceFreshness] = Field(default_factory=list)
    source_ages_seconds: dict[str, float | None] = Field(default_factory=dict)
    fetched_at: datetime | None = None
    source_age_seconds: float | None = Field(default=None, ge=0)
    stale: bool = False
    fixture_mode: bool = False
    unknown_coverage: bool = False
    observed_horizon_end: datetime | None = None
    horizon_status: Literal[
        "within_observed_horizon", "beyond_observed_horizon", "historical_not_covered", "unknown"
    ] = "unknown"
    coverage: str = "Netherlands road works only; footpaths, trails, parks and forestry closures are not established as covered."
    distance_crs: str | None = None
    distance_method: str = (
        "projected line operations; disconnected route segments are measured separately"
    )
