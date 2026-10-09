"""Bounded reader for the public NDW DATEX II v3 situation feeds.

Only directly supplied WGS84 GML lines are decoded here. Alert-C references are
retained as unresolved because resolving them requires the matching reference tables.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.closures import ClosureEvent, ClosureSourceError
from outdoor_agent.models.routes import Geometry
from outdoor_agent.models.time import parse_event_time
from outdoor_agent.providers.http import FetchResult, HTTPClient

MAX_COMPRESSED_BYTES = 24 * 1024 * 1024
MAX_XML_BYTES = 256 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_POINTS_PER_EVENT = 10_000


@dataclass
class FeedSnapshot:
    events: list[ClosureEvent]
    fetched_at: datetime
    source_timestamp: datetime | None
    stale: bool
    from_cache: bool
    source: str
    warnings: list[str]
    fixture_mode: bool = False


@dataclass
class FeedFetchResult:
    snapshots: list[FeedSnapshot]
    errors: list[ClosureSourceError]


def _local(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1].split(":")[-1]


def _text(element: Element, *names: str) -> str | None:
    wanted = set(names)
    for child in element.iter():
        if _local(child) in wanted and child.text and child.text.strip():
            return child.text.strip()
    return None


def _first_descendant(element: Element, name: str) -> Element | None:
    return next((child for child in element.iter() if _local(child) == name), None)


def _geometry(
    record: Element,
) -> tuple[
    Geometry | None, Literal["precise", "approximate", "incomplete", "unresolved"], str | None
]:
    segments: list[list[tuple[float, float]]] = []
    problems: list[str] = []
    line_count = 0
    point_count = 0
    for line in record.iter():
        if _local(line) != "gmlLineString":
            continue
        line_count += 1
        pos_list = _text(line, "posList")
        if not pos_list:
            problems.append("GML line has no posList.")
            continue
        try:
            numbers = [float(number) for number in pos_list.split()]
            srs = line.attrib.get("srsName", "").lower()
            pos_element = _first_descendant(line, "posList")
            dimension = int(
                line.attrib.get(
                    "srsDimension",
                    pos_element.attrib.get("srsDimension", "2") if pos_element is not None else "2",
                )
            )
            if dimension != 2 or len(numbers) < 4 or len(numbers) % 2:
                problems.append("GML line has unsupported coordinate dimensions or count.")
                continue
            if srs in {
                "urn:ogc:def:crs:ogc::crs84",
                "urn:ogc:def:crs:ogc:1.3:crs84",
                "crs84",
                "ogc:crs84",
                "crs:84",
            }:
                axis_order = "lonlat"
            elif srs in {"wgs 84", "epsg:4326", "urn:ogc:def:crs:epsg::4326"}:
                axis_order = "latlon"
            else:
                problems.append(f"Unsupported GML CRS: {srs or 'missing'}.")
                continue
            points = (
                [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)]
                if axis_order == "lonlat"
                else [(numbers[i + 1], numbers[i]) for i in range(0, len(numbers), 2)]
            )
            if len(points) > MAX_POINTS_PER_EVENT:
                return None, "incomplete", "Direct GML line exceeds point limit."
            if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in points):
                problems.append("GML line contains coordinates outside WGS84 bounds.")
                continue
            point_count += len(points)
            if point_count > MAX_POINTS_PER_EVENT:
                return None, "incomplete", "Event geometry exceeds point limit."
            segments.append(points)
        except (ValueError, TypeError):
            problems.append("GML line has malformed coordinates.")
            continue
    if not segments:
        message = "; ".join(problems) if problems else "No supported direct GML line."
        return (
            None,
            "unresolved",
            message + " Alert-C and other location references remain unresolved.",
        )
    members = sum(1 for item in record.iter() if _local(item) == "locationContainedInItinerary")
    location_types = {
        item.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type", "").split(":")[-1]
        for item in record.iter()
        if item.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type")
    }
    has_unresolved_member = any("alertc" in name.lower() for name in location_types)
    if members > line_count or has_unresolved_member:
        problems.append("Some indexed location members have no decoded direct geometry.")
    if len(segments) == 1:
        geometry = Geometry(type="LineString", coordinates=segments[0])
    else:
        geometry = Geometry(type="MultiLineString", coordinates=segments)
    quality: Literal["precise", "approximate", "incomplete", "unresolved"] = (
        "approximate"
        if has_unresolved_member or members > line_count
        else "incomplete"
        if problems
        else "precise"
    )
    return geometry, quality, "; ".join(dict.fromkeys(problems)) or None


def _location_reference(location: Element | None) -> tuple[str | None, str | None]:
    if location is None:
        return None, None
    relevant = {
        "alertclocationcountrycode",
        "alertclocationtablenumber",
        "alertclocationtableversion",
        "specificlocation",
        "offsetdistance",
        "alertcdirectioncoded",
        "alertcaffecteddirection",
    }
    references: list[str] = []
    types: list[str] = []
    for item in location.iter():
        kind = item.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type")
        if kind:
            kind = kind.split(":")[-1]
            if kind not in types:
                types.append(kind)
        name = _local(item).lower()
        value = (item.text or "").strip()
        if value and name in relevant:
            references.append(f"{_local(item)}={value}")
    if location.attrib.get("id"):
        references.insert(0, f"id={location.attrib['id']}")
    type_text = ",".join(types) or None
    if references or type_text:
        summary = f"types={type_text or 'unknown'}; " + "; ".join(references)
        return summary[:2000], type_text
    return "locationReference present; no supported direct geometry or reference fields", type_text


def _modes(
    record: Element, record_type: str, restriction: str
) -> list[Literal["road", "bike", "foot", "other"]]:
    modes: set[Literal["road", "bike", "foot", "other"]] = set()
    # DATEX forVehiclesWithCharacteristicsOf/vehicleType is affirmative mode evidence.
    vehicle_types = {
        (element.text or "").strip().lower()
        for element in record.iter()
        if _local(element) == "vehicleType" and element.text
    }
    if "bicycle" in vehicle_types:
        modes.add("bike")
    if any(value in {"pedestrian", "pedestrians", "foot", "walking"} for value in vehicle_types):
        modes.add("foot")
    pedestrian_types = {
        (element.text or "").strip().lower()
        for element in record.iter()
        if _local(element) == "pedestrianType" and element.text
    }
    if pedestrian_types.intersection({"pedestrian", "pedestrians", "foot", "walking"}):
        modes.add("foot")
    if vehicle_types.intersection({"car", "bus", "lorry", "motorcycle", "moped", "van", "taxi"}):
        modes.add("road")
    # Only exact, unambiguous closure labels count. Free-text descriptions are
    # intentionally ignored because phrases such as “bicycles allowed” are negation.
    closure_label = "".join(character for character in restriction.lower() if character.isalnum())
    if closure_label in {"cyclewayclosure", "cyclewayclosures", "bicycleclosure", "cyclistsclosed"}:
        modes.add("bike")
    if closure_label in {
        "footpathclosure",
        "pedestrianclosure",
        "walkwayclosure",
        "footwayclosure",
    }:
        modes.add("foot")
    record_label = "".join(character for character in record_type.lower() if character.isalnum())
    if record_label == "roadorcarriagewayorlanemanagement":
        modes.add("road")
    if record_label in {"cyclewayclosure", "bicycleclosure"}:
        modes.add("bike")
    if record_label in {"footpathclosure", "pedestrianclosure", "walkwayclosure"}:
        modes.add("foot")
    return cast(list[Literal["road", "bike", "foot", "other"]], sorted(modes))


def _event(
    situation: Element, observed_at: datetime, timezone: str, source: str
) -> list[ClosureEvent]:
    sid = situation.attrib.get("id", "")
    if not sid:
        sid = f"unidentified-{hashlib.sha256(ElementTree.tostring(situation)).hexdigest()[:24]}"
    issuer = _text(situation, "value", "sourceName", "organisationName")
    severity = _text(situation, "overallSeverity")
    published = parse_event_time(
        _text(situation, "situationVersionTime", "situationRecordVersionTime"), timezone
    )
    result: list[ClosureEvent] = []
    records = [item for item in situation.iter() if _local(item) == "situationRecord"]
    for record in records:
        event_id = (
            record.attrib.get("id")
            or f"unidentified-{hashlib.sha256(ElementTree.tostring(record)).hexdigest()[:24]}"
        )
        record_type = record.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
        record_type = record_type.split(":")[-1] or "situationRecord"
        record_published = parse_event_time(_text(record, "situationRecordVersionTime"), timezone)
        restriction = (
            _text(record, "roadOrCarriagewayOrLaneManagementType", "typeOfRoadworks", "causeType")
            or record_type
        )
        start = parse_event_time(
            _text(record, "overallStartTime", "validityTimeSpecificationStart"), timezone
        )
        end = parse_event_time(
            _text(record, "overallEndTime", "validityTimeSpecificationEnd"), timezone
        )
        recurrence_items = [
            item
            for item in record.iter()
            if _local(item) in {"recurringTimePeriodOfDay", "recurringDayWeekMonthPeriod"}
        ]
        recurrence = (
            " ".join(
                " ".join(text.strip() for text in item.itertext() if text and text.strip())
                for item in recurrence_items
            )
            or None
        )
        location = _first_descendant(record, "locationReference")
        location_reference, location_type = _location_reference(location)
        geom, geometry_quality, geom_problem = _geometry(record)
        modes = _modes(record, record_type, restriction)
        descr = " ".join(
            part
            for part in (restriction, _text(record, "roadName", "publicEventName", "causeType"))
            if part
        )
        status = _text(record, "operatorActionStatus", "informationStatus") or "active"
        if status.lower() in {"cancelled", "cancelledinpublication"}:
            status = "cancelled"
        has_schedule = bool(recurrence_items)
        timing_quality: Literal["precise", "unknown"] = (
            "precise" if start is not None and end is not None and not has_schedule else "unknown"
        )
        result.append(
            ClosureEvent(
                id=event_id,
                provider="ndw",
                source_url=source if source.startswith("https://") else None,
                description=descr or record_type,
                issuer=issuer,
                geometry=geom,
                location_reference=location_reference,
                geometry_quality=geometry_quality,
                severity=severity,
                starts_at=start,
                ends_at=end,
                recurrence=recurrence,
                timezone=timezone,
                affected_modes=modes,  # empty means mode evidence was absent
                restriction_type=restriction,
                direction=_text(record, "direction", "alertCDirectionCoded"),
                status=status,
                observed_at=observed_at,
                published_at=record_published or published,
                raw_metadata_reference=f"ndw:{sid}",
                timing_quality=timing_quality,
                location_reference_type=location_type,
                restriction_evidence=[
                    record_type,
                    restriction,
                    *([geom_problem] if geom_problem else []),
                ],
            )
        )
    if not records:
        result.append(
            ClosureEvent(
                id=sid,
                provider="ndw",
                description="Situation without a supported record",
                issuer=issuer,
                location_reference=sid,
                geometry_quality="unresolved",
                severity=severity,
                restriction_type="unknown",
                status="unknown",
                observed_at=observed_at,
                published_at=published,
                raw_metadata_reference=f"ndw:{sid}",
                restriction_evidence=["Situation contained no situationRecord."],
            )
        )
    return result


def _decompressed_stream(content: bytes) -> Any:
    source: Any = io.BytesIO(content)
    if content.startswith(b"\x1f\x8b"):
        source = gzip.GzipFile(fileobj=source)
    output: Any = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    total = 0
    while chunk := source.read(64 * 1024):
        total += len(chunk)
        if total > MAX_XML_BYTES:
            output.close()
            raise ValueError("Decompressed NDW feed exceeds 256 MiB.")
        output.write(chunk)
    output.seek(0)
    return output


def parse_feed(
    content: bytes,
    *,
    fetched_at: datetime | None = None,
    timezone: str = "Europe/Amsterdam",
    source: str = "fixture://ndw",
) -> FeedSnapshot:
    """Parse DATEX II v3 SituationPublication using bounded gzip and iterparse."""
    if len(content) > MAX_COMPRESSED_BYTES:
        raise ValueError("Compressed NDW feed exceeds 24 MiB.")
    observed = fetched_at or datetime.now(UTC)
    stream: Any = _decompressed_stream(content)
    events: list[ClosureEvent] = []
    publication: datetime | None = None
    version_ok = False
    warnings: list[str] = []
    try:
        iterator = ElementTree.iterparse(
            stream, events=("start", "end"), forbid_dtd=True, forbid_entities=True
        )
        for action, element in iterator:
            if action == "start" and _local(element) == "messageContainer":
                version_ok = element.attrib.get("modelBaseVersion") == "3"
            elif action == "end" and _local(element) == "publicationTime":
                publication = parse_event_time(element.text, timezone)
            elif action == "end" and _local(element) == "situation":
                record_count = sum(
                    1 for item in element.iter() if _local(item) == "situationRecord"
                )
                if len(events) + record_count > MAX_EVENTS:
                    raise ValueError("NDW feed exceeds 100,000 situation records.")
                events.extend(_event(element, observed, timezone, source))
                element.clear()
        if not version_ok:
            raise ValueError("Expected DATEX II v3 messageContainer.")
    except (DefusedXmlException, ElementTree.ParseError) as exc:
        raise ValueError("NDW feed is malformed or contains unsafe XML.") from exc
    finally:
        stream.close()
    unresolved = sum(event.geometry is None for event in events)
    if unresolved:
        warnings.append(
            f"{unresolved} feed record(s) have unresolved or unsupported geometry; Alert-C was not decoded."
        )
    if publication is None:
        warnings.append(
            "Feed publicationTime is missing or unparseable; fetched time is the only freshness evidence."
        )
    return FeedSnapshot(events, observed, publication, False, False, source, warnings)


async def fetch_feeds(settings: Settings) -> FeedFetchResult:
    """Fetch current and planned feeds; individual failures remain visible to caller."""
    if "ndw" in settings.disabled_providers:
        raise OutdoorError(
            "PROVIDER_DISABLED", "NDW is disabled in configuration.", 5, provider="ndw"
        )
    snapshots: list[FeedSnapshot] = []
    errors: list[ClosureSourceError] = []
    async with HTTPClient(settings) as client:
        for url, label in (
            (settings.ndw_closures_url, "current closures"),
            (settings.ndw_planned_url, "planned works"),
        ):
            try:
                result: FetchResult = await client.fetch(
                    "GET",
                    url,
                    provider="ndw",
                    max_bytes=MAX_COMPRESSED_BYTES,
                    ttl=settings.ndw_cache_ttl_seconds,
                    persist=True,
                    min_interval=0.25,
                )
                snapshot = parse_feed(
                    result.content,
                    fetched_at=result.fetched_at,
                    timezone=settings.default_timezone,
                    source=url,
                )
                snapshot.stale = result.stale
                snapshot.from_cache = result.from_cache
                snapshots.append(snapshot)
            except Exception as exc:
                # Do not expose provider response bodies, URLs with secrets, or XML.
                code = getattr(
                    exc,
                    "code",
                    "INVALID_FEED" if isinstance(exc, ValueError) else "PROVIDER_UNAVAILABLE",
                )
                errors.append(
                    ClosureSourceError(
                        source=url,
                        code=str(code),
                        message=f"NDW {label} feed unavailable or invalid.",
                    )
                )
    return FeedFetchResult(snapshots=snapshots, errors=errors)


def parse_fixture(path: Path, *, timezone: str = "Europe/Amsterdam") -> FeedSnapshot:
    """Explicit local fixture input; marked so it can never be mistaken for live data."""
    if not path.is_file() or path.stat().st_size > MAX_COMPRESSED_BYTES:
        raise ValueError("Fixture must be a readable file within the compressed feed limit.")
    content = path.read_bytes()
    snapshot = parse_feed(content, timezone=timezone, source=f"fixture://{path.name}")
    snapshot.fixture_mode = True
    snapshot.warnings.append("Fixture feed supplied explicitly; this result is demonstration data.")
    return snapshot
