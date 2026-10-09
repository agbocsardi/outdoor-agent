"""Optional, bounded OpenStreetMap access and construction evidence via Overpass."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.closures import ClosureEvent, ClosureSourceError
from outdoor_agent.models.routes import Geometry
from outdoor_agent.providers.base import ClosureFeedResult
from outdoor_agent.providers.http import FetchResult, HTTPClient
from outdoor_agent.providers.ndw import FeedSnapshot
from outdoor_agent.providers.registry import Provider, registry

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ELEMENTS = 1_000
MAX_BBOX_AREA_KM2 = 500
OSM_SOURCE = "https://www.openstreetmap.org/"
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)."


def _query_bbox(
    bbox: tuple[float, float, float, float], buffer_m: float
) -> tuple[float, float, float, float]:
    """Expand a lon/lat route bbox; return Overpass south,west,north,east order."""
    west, south, east, north = bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise OutdoorError("INVALID_BOUNDS", "OSM restriction bounds are invalid.", provider="osm")
    mid_lat = (south + north) / 2
    lat_delta = buffer_m / 110_574
    lon_delta = buffer_m / max(1.0, 111_320 * abs(math.cos(math.radians(mid_lat))))
    south = max(-90.0, south - lat_delta)
    north = min(90.0, north + lat_delta)
    west = max(-180.0, west - lon_delta)
    east = min(180.0, east + lon_delta)
    height_km = (north - south) * 110.574
    width_km = (east - west) * 111.32 * abs(math.cos(math.radians(mid_lat)))
    if height_km * width_km > MAX_BBOX_AREA_KM2:
        raise OutdoorError(
            "OSM_BOUNDS_TOO_LARGE",
            "Route bounding box is too large for a bounded OSM restriction query.",
            4,
            provider="osm",
        )
    return south, west, north, east


def overpass_restriction_query(
    bbox: tuple[float, float, float, float], buffer_m: float = 20
) -> str:
    south, west, north, east = _query_bbox(bbox, buffer_m)
    bounds = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    filters = (
        '["access"~"^(no|private)$"]',
        '["foot"~"^(no|private)$"]',
        '["bicycle"~"^(no|private)$"]',
        '["highway"="construction"]',
        '["construction"]',
        '["access:conditional"]',
        '["foot:conditional"]',
        '["bicycle:conditional"]',
    )
    selectors = "\n".join(f"way{tag_filter}({bounds});" for tag_filter in filters)
    return f"[out:json][timeout:20];\n({selectors}\n);\nout tags geom {MAX_ELEMENTS};"


def _way_event(element: dict[str, Any], observed_at: datetime) -> ClosureEvent | None:
    if element.get("type") != "way" or not isinstance(element.get("id"), int):
        return None
    tags = element.get("tags")
    points = element.get("geometry")
    if not isinstance(tags, dict) or not isinstance(points, list):
        return None
    coordinates: list[tuple[float, float]] = []
    if len(points) < 2 or len(points) > 10_000:
        return None
    for point in points:
        if not isinstance(point, dict):
            return None
        try:
            lat = float(point["lat"])
            lon = float(point["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        coordinates.append((lon, lat))

    modes: set[str] = set()
    evidence: list[str] = []
    access = str(tags.get("access", "")).strip().lower()
    access_restricted = access in {"no", "private"}
    for key, mode in (("foot", "foot"), ("bicycle", "bike")):
        value = str(tags.get(key, "")).strip().lower()
        if value in {"no", "private"} or (not value and access_restricted):
            modes.add(mode)
            evidence.append(f"{key}={value}" if value else f"access={access} (applies to {key})")
    if access_restricted:
        modes.add("road")
        evidence.append(f"access={access}")

    construction_value = str(tags.get("construction", "")).strip().lower()
    construction = str(tags.get("highway", "")).lower() == "construction" or (
        construction_value not in {"", "no", "false", "0"}
    )
    if construction:
        evidence.extend(f"{key}={tags[key]}" for key in ("highway", "construction") if key in tags)
    conditional = [
        f"{key}={str(tags[key])[:160]}"
        for key in ("access:conditional", "foot:conditional", "bicycle:conditional")
        if key in tags
    ]
    evidence.extend(conditional)
    if not modes and not construction and not conditional:
        return None

    grade_evidence: list[str] = []
    for key in ("bridge", "tunnel"):
        value = str(tags.get(key, "")).strip().lower()
        if value in {"yes", "true", "1"}:
            grade_evidence.append(f"{key}={value}")
    if "layer" in tags:
        grade_evidence.append(f"layer={str(tags['layer'])[:40]}")

    way_id = element["id"]
    name = str(tags.get("name") or tags.get("highway") or "OSM way")[:120]
    restriction = "construction" if construction else "access restriction"
    status = "active" if modes or construction else "unknown"
    return ClosureEvent(
        id=f"osm-way-{way_id}",
        provider="osm",
        source_url=f"https://www.openstreetmap.org/way/{way_id}",
        description=f"{restriction}: {name}",
        geometry=Geometry(type="LineString", coordinates=coordinates),
        geometry_quality="precise",
        affected_modes=sorted(modes),  # type: ignore[arg-type]
        mode_evidence="explicit" if modes else "scope" if construction else "unknown",
        restriction_type=restriction,
        status=status,
        observed_at=observed_at,
        timing_quality="unknown",
        restriction_evidence=[*evidence, OSM_ATTRIBUTION],
        raw_metadata_reference=f"osm:way:{way_id}",
        grade_separation_evidence=grade_evidence,
    )


class OSMRestrictionsProvider:
    """Fetch local OSM restriction evidence; it does not claim complete trail coverage."""

    declaration: Provider = next(item for item in registry() if item.provider == "osm")

    def __init__(self, settings: Settings, http: HTTPClient | None = None) -> None:
        self.settings = settings
        self.http = http

    async def list_events(
        self,
        bbox: tuple[float, float, float, float],
        window: str,
        mode: str,
    ) -> ClosureFeedResult:
        del window, mode  # OSM tags provide no reliable temporal validity or conditional grammar.
        if "osm" in self.settings.disabled_providers:
            return ClosureFeedResult(
                snapshots=[],
                errors=[
                    ClosureSourceError(
                        source="overpass",
                        code="PROVIDER_DISABLED",
                        message="Optional OSM restriction checking is disabled.",
                    )
                ],
            )
        own_client = self.http is None
        try:
            if own_client:
                async with HTTPClient(self.settings) as client:
                    fetched = await self._fetch(client, bbox)
            else:
                assert self.http is not None
                fetched = await self._fetch(self.http, bbox)
            snapshot, errors = fetched
            return ClosureFeedResult(snapshots=[snapshot], errors=errors)
        except Exception as exc:
            code = getattr(exc, "code", "PROVIDER_UNAVAILABLE")
            return ClosureFeedResult(
                snapshots=[],
                errors=[
                    ClosureSourceError(
                        source="overpass",
                        code=str(code),
                        message="Optional OSM restriction data is unavailable; trail coverage remains unknown.",
                    )
                ],
            )

    async def _fetch(
        self, client: HTTPClient, bbox: tuple[float, float, float, float]
    ) -> tuple[FeedSnapshot, list[ClosureSourceError]]:
        result: FetchResult = await client.fetch(
            "POST",
            self.settings.overpass_url,
            provider="overpass",
            data={"data": overpass_restriction_query(bbox, self.settings.closure_buffer_m)},
            max_bytes=MAX_RESPONSE_BYTES,
            ttl=self.settings.discovery_cache_ttl_seconds,
            persist=True,
            min_interval=2.0,
        )
        try:
            document = json.loads(result.content)
            elements = document["elements"]
            if not isinstance(elements, list):
                raise ValueError("elements is not a list")
        except (ValueError, TypeError, KeyError) as exc:
            raise OutdoorError(
                "PROVIDER_INVALID_RESPONSE",
                "Overpass returned malformed closure data.",
                4,
                provider="osm",
            ) from exc
        warnings = [
            "Optional OpenStreetMap access/construction evidence; this does not establish full trail coverage.",
            OSM_ATTRIBUTION,
        ]
        errors: list[ClosureSourceError] = []
        if document.get("remark"):
            warnings.append("Overpass returned a partial response.")
            errors.append(
                ClosureSourceError(
                    source="overpass",
                    code="PARTIAL_RESPONSE",
                    message="Overpass marked the restriction response partial.",
                )
            )
        if len(elements) >= MAX_ELEMENTS:
            warnings.append(
                "Overpass response reached the way limit; additional restrictions may be omitted."
            )
            errors.append(
                ClosureSourceError(
                    source="overpass",
                    code="RESULT_LIMIT",
                    message="Overpass restriction response reached its bounded result limit.",
                )
            )
        events = [
            event
            for element in elements[:MAX_ELEMENTS]
            if isinstance(element, dict)
            if (event := _way_event(element, result.fetched_at)) is not None
        ]
        snapshot = FeedSnapshot(
            events=events,
            fetched_at=result.fetched_at,
            source_timestamp=None,
            stale=result.stale,
            from_cache=result.from_cache,
            source=OSM_SOURCE,
            warnings=warnings,
            response_headers=result.headers,
        )
        return snapshot, errors
