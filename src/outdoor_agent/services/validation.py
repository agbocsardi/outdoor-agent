"""Closure assessment orchestration: source freshness, time, mode and geometry."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.closures import compare_prepared, finding_for, prepare_route
from outdoor_agent.models.closures import (
    ClosureEvent,
    ClosureFinding,
    ClosureReport,
    ClosureSourceError,
)
from outdoor_agent.models.routes import Activity, Coordinate, Route
from outdoor_agent.models.time import overlaps, parse_window
from outdoor_agent.providers.ndw import FeedFetchResult, fetch_feeds, parse_fixture


def _aggregate(findings: list[ClosureFinding], *, unknown_coverage: bool, failed: bool) -> str:
    classes = {finding.classification for finding in findings}
    if "known_conflict" in classes:
        return "known_conflict"
    if "possible_conflict" in classes:
        return "possible_conflict"
    if failed:
        return "check_failed"
    if unknown_coverage or "coverage_unknown" in classes:
        return "coverage_unknown"
    return "no_known_conflict"


async def assess_route(
    settings: Settings,
    route: Route,
    date: str,
    activity: Activity,
    feed_file: Path | None = None,
    *,
    feed_result: FeedFetchResult | None = None,
) -> ClosureReport:
    """Check one route and retain unresolved feed coverage as first-class output.

    `feed_file` explicitly opts into fixture/demo data and is always exposed
    through `fixture_mode`; the normal path fetches both current and planned NDW feeds.
    """
    if feed_file is None and "ndw" in settings.disabled_providers:
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
            feeds = feed_result.snapshots
            source_errors = feed_result.errors
        elif feed_file:
            feeds = [parse_fixture(feed_file, timezone=settings.default_timezone)]
        else:
            fetched = await fetch_feeds(settings)
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

    warnings = [warning for feed in feeds for warning in feed.warnings]
    partial = bool(source_errors) and bool(feeds) and not fixture_mode
    failed = not feeds and not fixture_mode
    if partial:
        warnings.append("Only part of the expected NDW current/planned feed set was available.")
    source_ages: dict[str, float | None] = {
        feed.source: max(
            0.0,
            (now - (feed.source_timestamp or feed.fetched_at).astimezone(UTC)).total_seconds(),
        )
        for feed in feeds
    }
    age_values = [value for value in source_ages.values() if value is not None]
    age = max(age_values) if age_values else None
    age_stamps = [feed.source_timestamp or feed.fetched_at for feed in feeds]
    oldest = min(age_stamps) if age_stamps else None
    stale = any(feed.stale for feed in feeds) or (
        age is not None and age > settings.ndw_max_age_seconds
    )
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
    findings: list[ClosureFinding] = []
    matched_events: list[ClosureEvent] = []
    unresolved = False
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
            and event.affected_modes
            and not activity_modes.intersection(event.affected_modes)
        ):
            # Explicitly incompatible restrictions (for example car-only while
            # using a separated cycle path) are not route conflicts.
            continue
        if event.geometry is None:
            unresolved = True
            findings.append(
                ClosureFinding(
                    route_id=route.id,
                    event_id=event.id,
                    severity=event.severity,
                    classification="coverage_unknown",
                    reason="NDW record has no decoded geometry; its relation to the route cannot be determined.",
                    temporal_overlap=temporal,
                    mode_overlap=(
                        bool(activity_modes.intersection(event.affected_modes))
                        if activity_modes is not None and event.affected_modes
                        else None
                    ),
                    geometry_confidence="unknown",
                    source_url=event.source_url,
                    evidence=event.restriction_evidence,
                    limitations=[
                        "Location reference is unresolved; Alert-C decoding is not implemented."
                    ],
                )
            )
            matched_events.append(event)
            continue
        match = compare_prepared(prepared_route, event) if prepared_route is not None else None
        if match is None:
            continue
        findings.append(
            finding_for(assessed_route, event, match, temporal, settings.closure_buffer_m)
        )
        matched_events.append(event)

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
    status = _aggregate(findings, unknown_coverage=unresolved or partial, failed=failed)
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
