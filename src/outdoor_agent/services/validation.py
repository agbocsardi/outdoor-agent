"""Closure assessment orchestration: source freshness, time, mode and geometry."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, cast

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.closures import compare_prepared, finding_for, prepare_route
from outdoor_agent.models.closures import (
    Assessment,
    ClosureCoverageSummary,
    ClosureEvent,
    ClosureFinding,
    ClosureReport,
    ClosureSourceError,
    ClosureSourceFreshness,
)
from outdoor_agent.models.routes import Activity, Coordinate, Route
from outdoor_agent.models.time import overlaps, parse_window
from outdoor_agent.providers.ndw import FeedFetchResult, fetch_feeds, parse_fixture
from outdoor_agent.providers.ndw_adapter import NDWClosureProvider
from outdoor_agent.providers.osm_restrictions import OSMRestrictionsProvider

MAX_ROUTE_FINDINGS = 100
MAX_SUMMARY_SAMPLE_IDS = 5
SummaryReason = Literal["unresolved_location", "route_geometry_unavailable", "route_finding_limit"]


def _aggregate(
    findings: list[ClosureFinding],
    *,
    unknown_coverage: bool,
    failed: bool,
    observed_classes: set[Assessment] | None = None,
) -> str:
    classes = {finding.classification for finding in findings}
    classes.update(observed_classes or set())
    if "known_conflict" in classes:
        return "known_conflict"
    if "possible_conflict" in classes:
        return "possible_conflict"
    if failed:
        return "check_failed"
    if unknown_coverage or "coverage_unknown" in classes:
        return "coverage_unknown"
    return "no_known_conflict"


def _event_fingerprint(event: ClosureEvent) -> str:
    payload = event.model_dump(mode="json", exclude={"observed_at", "published_at"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _coverage_summaries(
    entries: dict[tuple[str, SummaryReason], list[tuple[str, str]]],
) -> list[ClosureCoverageSummary]:
    summaries: list[ClosureCoverageSummary] = []
    overflow: dict[SummaryReason, list[tuple[str, str]]] = {}
    active_reasons = sorted({reason for _, reason in entries})
    detail_limit = max(0, 12 - len(active_reasons))
    for index, ((source, reason), values) in enumerate(sorted(entries.items())):
        if index >= detail_limit:
            overflow.setdefault(reason, []).extend(values)
            continue
        summaries.append(_make_summary(source, reason, values))
    for reason, values in sorted(overflow.items()):
        summaries.append(_make_summary("other_sources", reason, values))
    return summaries


def _make_summary(
    source: str, reason: SummaryReason, values: list[tuple[str, str]]
) -> ClosureCoverageSummary:
    digest = hashlib.sha256()
    for _, fingerprint in sorted(values, key=lambda value: (value[0], value[1])):
        digest.update(bytes.fromhex(fingerprint))
    return ClosureCoverageSummary(
        source=source[:256],
        reason=reason,
        count=len(values),
        sample_event_ids=sorted({event_id[:160] for event_id, _ in values})[
            :MAX_SUMMARY_SAMPLE_IDS
        ],
        digest=digest.hexdigest(),
    )


def _summary_source(event: ClosureEvent) -> str:
    # NDW's two feed URLs distinguish current/planned records. OSM event URLs are
    # per-way links, so aggregate those by provider to keep report rows bounded.
    if event.provider == "ndw":
        source = (event.source_url or "ndw")[:256]
        return f"source:{source}" if source == "other_sources" else source
    if event.provider == "osm":
        return "osm"
    return "other"


def _route_bbox(route: Route) -> tuple[float, float, float, float] | None:
    if route.geometry is None:
        return None
    points = (
        [
            point
            for line in cast(list[list[Coordinate]], route.geometry.coordinates)
            for point in line
        ]
        if route.geometry.type == "MultiLineString"
        else cast(list[Coordinate], route.geometry.coordinates)
    )
    if not points:
        return None
    west = min(point[0] for point in points)
    east = max(point[0] for point in points)
    south = min(point[1] for point in points)
    north = max(point[1] for point in points)
    if west == east:
        west, east = west - 1e-7, east + 1e-7
    if south == north:
        south, north = south - 1e-7, north + 1e-7
    return west, south, east, north


async def assess_route(
    settings: Settings,
    route: Route,
    date: str,
    activity: Activity,
    feed_file: Path | None = None,
    *,
    feed_result: FeedFetchResult | None = None,
    osm_restrictions: bool = False,
) -> ClosureReport:
    """Check one route and retain unresolved feed coverage as first-class output.

    `feed_file` explicitly opts into fixture/demo data and is always exposed
    through `fixture_mode`; the normal path fetches both current and planned NDW feeds.
    """
    ndw_disabled = feed_file is None and "ndw" in settings.disabled_providers
    if ndw_disabled and not osm_restrictions:
        raise OutdoorError(
            "PROVIDER_DISABLED", "NDW is disabled in configuration.", 5, provider="ndw"
        )
    now = datetime.now(UTC)
    try:
        window = parse_window(date, settings.default_timezone)
    except (ValueError, TypeError):
        return ClosureReport(
            route_id=route.id,
            activity=activity.value,
            requested_date=date,
            status="check_failed",
            source_status="failed",
            warnings=[
                "Requested date/time is invalid or ambiguous; supply an ISO date or a datetime with an explicit UTC offset."
            ],
        )

    fixture_mode = feed_file is not None
    source_errors: list[ClosureSourceError] = []
    try:
        if feed_result is not None:
            feeds = list(feed_result.snapshots)
            source_errors = list(feed_result.errors)
        elif feed_file:
            feeds = [parse_fixture(feed_file, timezone=settings.default_timezone)]
        elif ndw_disabled:
            feeds = []
            source_errors = [
                ClosureSourceError(
                    source="ndw",
                    code="PROVIDER_DISABLED",
                    message="NDW closure feeds are disabled; optional OSM restrictions were requested separately.",
                )
            ]
        else:
            fetched = await NDWClosureProvider(settings, fetcher=fetch_feeds).list_events(
                _route_bbox(route) or (0.0, 0.0, 0.000001, 0.000001), date, activity.value
            )
            feeds = fetched.snapshots
            source_errors = fetched.errors
    except Exception as exc:
        return ClosureReport(
            route_id=route.id,
            activity=activity.value,
            requested_date=date,
            status="check_failed",
            source_status="failed",
            request_precision=window.precision,
            request_timezone=window.timezone,
            window_start=window.start,
            window_end=window.end,
            fixture_mode=fixture_mode,
            warnings=[
                f"Closure source could not be read ({type(exc).__name__}); no safety conclusion is available."
            ],
        )

    osm_requested = osm_restrictions
    if osm_restrictions:
        bbox = _route_bbox(route)
        if bbox is None:
            source_errors.append(
                ClosureSourceError(
                    source="overpass",
                    code="ROUTE_GEOMETRY_MISSING",
                    message="OSM restrictions were requested but the route has no usable geometry.",
                )
            )
        else:
            osm_result = await OSMRestrictionsProvider(settings).list_events(
                bbox, date, activity.value
            )
            feeds.extend(osm_result.snapshots)
            source_errors.extend(osm_result.errors)

    warnings = [warning for feed in feeds for warning in feed.warnings]
    partial = bool(source_errors) and bool(feeds) and not fixture_mode
    failed = not feeds and not fixture_mode
    if partial:
        warnings.append(
            "One or more requested closure sources failed; combined coverage is incomplete."
        )
    if osm_requested:
        warnings.append(
            "OpenStreetMap access and construction tags are optional evidence only; unmapped and untimed trail closures remain unknown."
        )
    source_ages: dict[str, float | None] = {}
    source_freshness: list[ClosureSourceFreshness] = []
    freshness_ages: list[float] = []
    for feed in feeds:
        headers = feed.response_headers or {}
        publication_at = feed.source_timestamp
        last_modified = headers.get("last-modified")
        fallback_at = publication_at
        if fallback_at is None and last_modified:
            try:
                fallback_at = parsedate_to_datetime(last_modified)
                if fallback_at.tzinfo is None:
                    fallback_at = fallback_at.replace(tzinfo=UTC)
                warnings.append(
                    f"{feed.source}: publicationTime is unavailable; HTTP Last-Modified is used as fallback freshness evidence."
                )
            except (TypeError, ValueError, OverflowError):
                fallback_at = None
        if fallback_at is None:
            fallback_at = feed.fetched_at
            if publication_at is None:
                if feed.source.startswith("https://www.openstreetmap.org"):
                    warnings.append(
                        "OpenStreetMap response has no publication time; collection time is observation evidence only."
                    )
                else:
                    warnings.append(
                        f"{feed.source}: publicationTime and usable Last-Modified are unavailable; fetch time is the fallback freshness evidence."
                    )
        publication_age = (
            max(0.0, (now - publication_at.astimezone(UTC)).total_seconds())
            if publication_at is not None
            else None
        )
        source_age = max(0.0, (now - fallback_at.astimezone(UTC)).total_seconds())
        refresh_age = max(0.0, (now - feed.fetched_at.astimezone(UTC)).total_seconds())
        source_stale = feed.stale or source_age > settings.ndw_max_age_seconds
        source_ages[feed.source] = source_age
        freshness_ages.append(source_age)
        source_freshness.append(
            ClosureSourceFreshness(
                source=feed.source,
                last_successful_refresh=feed.fetched_at,
                publication_at=publication_at,
                etag=headers.get("etag"),
                last_modified=last_modified,
                from_cache=feed.from_cache,
                stale=source_stale,
                refresh_age_seconds=refresh_age,
                publication_age_seconds=publication_age,
            )
        )
    age = max(freshness_ages) if freshness_ages else None
    age_stamps = [feed.source_timestamp or feed.fetched_at for feed in feeds]
    oldest = min(age_stamps) if age_stamps else None
    stale = any(item.stale for item in source_freshness)
    if stale:
        warnings.append(
            "NDW feed data is stale; absence of findings cannot establish current conditions."
        )
    if route.geometry is None:
        route_points: list[Coordinate] = []
    elif route.geometry.type == "MultiLineString":
        route_points = [
            point
            for line in cast(list[list[Coordinate]], route.geometry.coordinates)
            for point in line
        ]
    else:
        route_points = cast(list[Coordinate], route.geometry.coordinates)
    outside_nl = route.geometry is not None and any(
        not (3 <= lon <= 8 and 50 <= lat <= 54) for lon, lat in route_points
    )
    if outside_nl:
        warnings.append(
            "Route has points outside the Netherlands; NDW coverage is not established there."
        )
    warnings.append(
        "NDW focuses on road traffic measures; trail, park, municipal path and forestry closure coverage is incomplete."
    )

    # Current and planned snapshots can contain revisions of the same DATEX record.
    # A newer cancellation must replace an older active revision, rather than coexist.
    latest: dict[str, tuple[datetime, ClosureEvent]] = {}
    for feed in feeds:
        for event in feed.events:
            revision = event.published_at or feed.source_timestamp or feed.fetched_at
            previous = latest.get(event.id)
            if previous is None or revision > previous[0]:
                latest[event.id] = (revision, event)
            elif revision == previous[0]:
                cancelled = {"cancelled", "cancelledinpublication"}
                if event.status.lower() in cancelled:
                    latest[event.id] = (revision, event)
                elif previous[1].status.lower() not in cancelled and event.model_dump(
                    exclude={"observed_at", "source_url"}
                ) != previous[1].model_dump(exclude={"observed_at", "source_url"}):
                    latest[event.id] = (
                        revision,
                        event.model_copy(update={"timing_quality": "unknown"}),
                    )
                    warnings.append(
                        "Conflicting NDW revisions with identical timestamps have uncertain validity."
                    )
    events = [event for _, event in latest.values()]
    active_ends = [
        event.ends_at
        for event in events
        if event.ends_at is not None
        and event.status.lower() not in {"cancelled", "cancelledinpublication"}
    ]
    observed_horizon = max(active_ends, default=None)
    if oldest is not None and window.end < oldest.astimezone(UTC):
        horizon_status = "historical_not_covered"
        warnings.append(
            "Requested time predates the fetched feed publication; historical coverage is unavailable."
        )
    elif observed_horizon is None:
        horizon_status = "unknown"
        warnings.append(
            "The feeds provide no usable event end time from which to estimate an observed horizon."
        )
    elif window.end <= observed_horizon:
        horizon_status = "within_observed_horizon"
    else:
        horizon_status = "beyond_observed_horizon"
        warnings.append(
            "Requested time is beyond the latest end time observed in the feeds; the feed horizon is not established."
        )

    assessed_route = route.model_copy(update={"activity": activity})
    prepared_route = prepare_route(assessed_route, settings.closure_buffer_m)
    retained: list[tuple[ClosureFinding, ClosureEvent]] = []
    summarized: dict[tuple[str, SummaryReason], list[tuple[str, str]]] = {}
    observed_classes: set[Assessment] = set()

    def summarize(event: ClosureEvent, reason: SummaryReason) -> None:
        summarized.setdefault((_summary_source(event), reason), []).append(
            (event.id, _event_fingerprint(event))
        )

    unresolved = osm_requested
    activity_modes = (
        {"bike"}
        if activity in {Activity.road, Activity.gravel, Activity.mtb}
        else {"foot"}
        if activity in {Activity.hiking, Activity.walking, Activity.trail_running, Activity.running}
        else None
    )
    explicit_modes = {
        mode
        for event in events
        for mode in event.affected_modes
        if event.status.lower() not in {"cancelled", "cancelledinpublication"}
    }
    if activity_modes is None:
        unresolved = True
        warnings.append("This activity has no NDW closure-mode mapping; coverage is unknown.")
    elif "foot" in activity_modes and "foot" not in explicit_modes:
        unresolved = True
        warnings.append(
            "NDW provides no explicit foot closure evidence in this response; trail and path coverage is unknown."
        )

    for event in events:
        if event.status.lower() in {"cancelled", "cancelledinpublication"}:
            continue
        temporal = overlaps(window, event.starts_at, event.ends_at)
        if temporal is False:
            continue
        if (
            activity_modes is not None
            and event.mode_evidence == "explicit"
            and event.affected_modes
            and not activity_modes.intersection(event.affected_modes)
        ):
            # Explicitly incompatible restrictions (for example car-only while
            # using a separated cycle path) are not route conflicts.
            continue
        if event.geometry is None:
            unresolved = True
            summarize(event, "unresolved_location")
            continue
        if prepared_route is None:
            unresolved = True
            summarize(event, "route_geometry_unavailable")
            continue
        match = compare_prepared(prepared_route, event)
        if match is None:
            continue
        finding = finding_for(assessed_route, event, match, temporal, settings.closure_buffer_m)
        classification = finding.classification
        observed_classes.add(classification)
        candidate = (finding, event)
        if len(retained) < MAX_ROUTE_FINDINGS:
            retained.append(candidate)
            continue

        def rank(item: tuple[ClosureFinding, ClosureEvent]) -> int:
            return {
                "known_conflict": 3,
                "possible_conflict": 2,
                "coverage_unknown": 1,
                "check_failed": 0,
                "no_known_conflict": 0,
            }[item[0].classification]

        weakest_index = min(range(len(retained)), key=lambda index: rank(retained[index]))
        if rank(candidate) > rank(retained[weakest_index]):
            displaced = retained[weakest_index][1]
            summarize(displaced, "route_finding_limit")
            retained[weakest_index] = candidate
        else:
            summarize(event, "route_finding_limit")
        unresolved = True

    findings = [finding for finding, _ in retained]
    matched_events = [event for _, event in retained]
    coverage_summaries = _coverage_summaries(summarized)
    if coverage_summaries:
        unlocated_count = sum(
            summary.count
            for summary in coverage_summaries
            if summary.reason in {"unresolved_location", "route_geometry_unavailable"}
        )
        overflow_count = sum(
            summary.count
            for summary in coverage_summaries
            if summary.reason == "route_finding_limit"
        )
        if unlocated_count:
            warnings.append(
                f"{unlocated_count} relevant event(s) without route-locatable geometry were summarized by source and reason."
            )
        if overflow_count:
            warnings.append(
                f"Route-specific findings are capped at {MAX_ROUTE_FINDINGS}; {overflow_count} additional matched event(s) were summarized."
            )

    if route.geometry is None:
        unresolved = True
        warnings.append("Route has no line geometry; spatial closure assessment was not possible.")
    if (
        stale
        or outside_nl
        or horizon_status != "within_observed_horizon"
        or any(feed.source_timestamp is None for feed in feeds)
    ):
        unresolved = True
    status = _aggregate(
        findings,
        unknown_coverage=unresolved or partial,
        failed=failed,
        observed_classes=observed_classes,
    )
    if stale and status == "no_known_conflict":
        status = "coverage_unknown"
    return ClosureReport(
        route_id=route.id,
        activity=activity.value,
        requested_date=date,
        request_precision=window.precision,
        request_timezone=window.timezone,
        window_start=window.start,
        window_end=window.end,
        status=status,  # type: ignore[arg-type]
        source_status=(
            "fixture" if fixture_mode else "failed" if failed else "partial" if partial else "ok"
        ),
        findings=findings,
        events=matched_events,
        warnings=list(dict.fromkeys(warnings)),
        sources=[feed.source for feed in feeds],
        source_errors=source_errors,
        source_freshness=source_freshness,
        coverage_summaries=coverage_summaries,
        finding_limit=MAX_ROUTE_FINDINGS,
        source_ages_seconds=source_ages,
        fetched_at=max((feed.fetched_at for feed in feeds), default=None),
        source_age_seconds=age,
        stale=stale,
        fixture_mode=fixture_mode,
        unknown_coverage=unresolved or partial or failed,
        observed_horizon_end=observed_horizon,
        horizon_status=horizon_status,  # type: ignore[arg-type]
        distance_crs=prepared_route.metric_crs if prepared_route is not None else None,
    )
