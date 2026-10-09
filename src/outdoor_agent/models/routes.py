"""Normalized WGS84 routes and lossless GPX inspection summaries."""

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from outdoor_agent.models.privacy import scrub_metadata


def now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Activity(StrEnum):
    hiking = "hiking"
    walking = "walking"
    trail_running = "trail_running"
    running = "running"
    road = "road"
    gravel = "gravel"
    mtb = "mtb"
    other = "other"


class SourceType(StrEnum):
    community = "community"
    official_trail = "official_trail"
    generated = "generated"
    unknown = "unknown"  # A local file alone cannot establish authorship.


Longitude = Annotated[float, Field(ge=-180, le=180)]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Coordinate = tuple[Longitude, Latitude]


class Geometry(Model):
    type: Literal["LineString", "MultiLineString"]
    coordinates: list[Coordinate] | list[list[Coordinate]]
    crs: Literal["EPSG:4326"] = "EPSG:4326"

    @model_validator(mode="after")
    def valid_lines(self) -> "Geometry":
        lines = [self.coordinates] if self.type == "LineString" else self.coordinates
        if not lines:
            raise ValueError("Geometry must contain a line")
        for line in lines:
            if len(line) < 2:
                raise ValueError("Each line must contain at least two coordinates")
            for point in line:
                if not isinstance(point, tuple) or len(point) != 2:
                    raise ValueError("Geometry type and coordinate nesting must agree")
                if not all(math.isfinite(value) for value in point):
                    raise ValueError("Coordinates must be finite")
        return self


class Metric(Model):
    value: float
    source_url: str
    observed_at: datetime


class Route(Model):
    id: str
    provider: str
    provider_route_id: str
    source_url: str | None = None
    source_type: SourceType = SourceType.unknown
    attribution: str
    name: str
    description: str | None = None
    activity: Activity = Activity.other
    geometry: Geometry | None = None
    start_lat: Latitude | None = None
    start_lon: Longitude | None = None
    end_lat: Latitude | None = None
    end_lon: Longitude | None = None
    distance_m: float | None = Field(default=None, ge=0)
    elevation_gain_m: float | None = Field(default=None, ge=0)
    is_loop: bool | None = None
    popularity: Metric | None = None
    rating: Metric | None = None
    reviews_count: Metric | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=now)
    geometry_quality: Literal["complete", "disconnected", "incomplete", "unknown"] = "unknown"
    gpx_access: Literal["available", "restricted", "unverified", "unavailable"] = "unverified"
    warnings: list[str] = Field(default_factory=list)
    # Only bounded, scrubbed scalar metadata is retained; no provider response blobs.
    raw_metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def private_metadata(self) -> "Route":
        sanitized = scrub_metadata(self.raw_metadata)
        if sanitized != self.raw_metadata:
            self.raw_metadata = sanitized
            warning = "Provider metadata was bounded or redacted before local storage."
            if warning not in self.warnings:
                self.warnings.append(warning)
        return self


class Waypoint(Model):
    lat: Latitude
    lon: Longitude
    elevation_m: float | None = None
    name: str | None = None


class GPXInspection(Model):
    route: Route
    sha256: str
    byte_count: int
    bounds: tuple[Longitude, Latitude, Longitude, Latitude]
    track_count: int
    track_segment_count: int
    route_count: int
    point_count: int
    segment_point_counts: list[int]
    waypoints: list[Waypoint]
    distance_method: Literal["WGS84 geodesic; segment gaps excluded"] = (
        "WGS84 geodesic; segment gaps excluded"
    )
    geometry_precision: Literal["original coordinates; no simplification"] = (
        "original coordinates; no simplification"
    )


class GPXArtifact(Model):
    route_id: str
    path: str
    sha256: str
    byte_count: int


class ImportResult(Model):
    inspection: GPXInspection
    artifact: GPXArtifact
    already_present: bool
