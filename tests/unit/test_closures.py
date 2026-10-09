from datetime import timedelta
from pathlib import Path

import pytest

from outdoor_agent.config import Settings
from outdoor_agent.geo.closures import (
    _metric_transformer,
    compare_geometry,
    compare_prepared,
    finding_for,
    prepare_route,
)
from outdoor_agent.models.closures import ClosureEvent
from outdoor_agent.models.routes import Activity, Geometry, Route
from outdoor_agent.services.validation import assess_route


def _route(activity: Activity = Activity.gravel, *, offset: float = 0) -> Route:
    return Route(
        id="route-1",
        provider="local",
        provider_route_id="route-1",
        attribution="local",
        name="test",
        activity=activity,
        geometry=Geometry(
            type="LineString", coordinates=[(5.1 + offset, 52.1), (5.1 + offset, 52.101)]
        ),
        geometry_quality="complete",
    )


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", **kwargs)


@pytest.mark.asyncio
async def test_precise_active_cycle_closure_is_known_and_fixture_is_explicit(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    report = await assess_route(
        _settings(tmp_path), _route(), "2026-10-11", Activity.gravel, fixture
    )
    assert report.status == "known_conflict"
    assert report.source_status == "fixture"
    assert report.fixture_mode is True
    assert report.request_precision == "date"
    assert report.request_timezone == "Europe/Amsterdam"
    assert report.window_start is not None
    assert report.window_end is not None
    assert report.events
    assert report.unknown_coverage is True
    assert report.distance_crs == "EPSG:28992"
    match = next(item for item in report.findings if item.event_id == "bike-closure-record")
    assert match.temporal_overlap is True
    assert match.mode_overlap is True
    assert match.distance_along_route_m is not None
    assert match.overlap_length_m is not None
    matched_event = next(item for item in report.events if item.id == match.event_id)
    assert matched_event.issuer == "Test municipality"
    assert matched_event.starts_at is not None and matched_event.ends_at is not None
    assert matched_event.description
    assert matched_event.affected_modes == ["bike"]
    assert matched_event.geometry is not None
    assert all(item.event_id != "expired-record" for item in report.findings)


@pytest.mark.asyncio
async def test_carriageway_only_event_is_not_applied_to_cycle_route(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    report = await assess_route(
        _settings(tmp_path), _route(), "2026-10-11", Activity.gravel, fixture
    )
    assert all(item.event_id != "road-only-record" for item in report.findings)


@pytest.mark.asyncio
async def test_road_scope_without_vehicle_evidence_is_retained_as_uncertain(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from outdoor_agent.providers.ndw import FeedFetchResult, FeedSnapshot

    now = datetime.now(UTC)
    event = ClosureEvent(
        id="road-scope",
        provider="ndw",
        description="Road carriageway works",
        geometry=Geometry(type="LineString", coordinates=[(5.1, 52.1), (5.1, 52.101)]),
        geometry_quality="precise",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=1),
        timing_quality="precise",
        mode_evidence="scope",
        restriction_type="carriagewayClosure",
        status="active",
    )
    snapshot = FeedSnapshot(
        events=[event],
        fetched_at=now,
        source_timestamp=now,
        stale=False,
        from_cache=False,
        source="https://feed.example/current",
        warnings=[],
    )
    report = await assess_route(
        _settings(tmp_path),
        _route(),
        now.date().isoformat(),
        Activity.gravel,
        feed_result=FeedFetchResult(snapshots=[snapshot], errors=[]),
    )
    assert any(item.event_id == event.id for item in report.findings)
    assert report.findings[0].classification == "possible_conflict"
    assert report.findings[0].mode_overlap is None


def test_parallel_route_15m_away_is_nearby_not_intersection() -> None:
    from outdoor_agent.providers.ndw import parse_fixture

    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    event = next(item for item in parse_fixture(fixture).events if item.id == "bike-closure-record")
    match = compare_geometry(_route(offset=0.00022), event, 20)
    assert match is not None
    assert 14 <= match.distance_m <= 16
    assert match.intersects is False


@pytest.mark.asyncio
async def test_feed_outage_never_becomes_no_known_conflict(tmp_path: Path) -> None:
    report = await assess_route(
        _settings(tmp_path, offline=True), _route(), "2026-10-11", Activity.gravel
    )
    assert report.status == "check_failed"
    assert report.source_status == "failed"
    assert report.unknown_coverage is True
    assert len(report.source_errors) == 2
    assert {error.code for error in report.source_errors} == {"OFFLINE_CACHE_MISS"}


def test_disconnected_segments_are_not_bridged_for_distance() -> None:
    route = _route()
    route.geometry = Geometry(
        type="MultiLineString",
        coordinates=[
            [(5.1, 52.1), (5.1, 52.101)],
            [(5.101, 52.1), (5.101, 52.101)],
        ],
    )
    event = ClosureEvent(
        id="between",
        provider="ndw",
        description="test",
        geometry=Geometry(type="LineString", coordinates=[(5.1005, 52.1), (5.1005, 52.101)]),
        geometry_quality="precise",
        restriction_type="cycleway",
        status="active",
    )
    match = compare_geometry(route, event, 100)
    assert match is not None
    # The route components stay separate; no artificial connecting edge is created.
    assert match.distance_m > 30


def test_southern_route_uses_southern_utm() -> None:
    route = Route(
        id="southern",
        provider="local",
        provider_route_id="southern",
        attribution="local",
        name="southern route",
        geometry=Geometry(type="LineString", coordinates=[(18, -33), (18.01, -33)]),
    )
    _, crs = _metric_transformer(route)
    assert crs == "EPSG:32734"


def test_wgs84_bbox_prefilter_skips_distant_event_before_projection(monkeypatch) -> None:
    import outdoor_agent.geo.closures as closure_geo

    calls = 0
    transform = closure_geo.shapely_transform

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return transform(*args, **kwargs)

    monkeypatch.setattr(closure_geo, "shapely_transform", counted)
    route = _route()
    prepared = prepare_route(route, 20)
    assert prepared is not None
    assert calls == 1
    far_event = ClosureEvent(
        id="far",
        provider="ndw",
        description="far",
        restriction_type="road",
        status="active",
        geometry=Geometry(type="LineString", coordinates=[(4, 52), (4.001, 52)]),
    )
    assert compare_prepared(prepared, far_event) is None
    assert calls == 1


def test_uncertain_grade_separation_stays_possible() -> None:
    from datetime import UTC, datetime

    event = ClosureEvent(
        id="bridge",
        provider="ndw",
        description="bike closure at bridge",
        affected_modes=["bike"],
        starts_at=datetime(2026, 10, 10, tzinfo=UTC),
        ends_at=datetime(2026, 10, 12, tzinfo=UTC),
        timing_quality="precise",
        geometry_quality="approximate",
        restriction_type="cycleway closure",
        status="active",
        geometry=Geometry(type="LineString", coordinates=[(5.1, 52.1), (5.1, 52.101)]),
    )
    match = compare_geometry(_route(), event, 20)
    assert match is not None
    finding = finding_for(_route(), event, match, True, 20)
    assert finding.classification == "possible_conflict"
    assert any("approximate" in issue for issue in finding.limitations)


def test_precise_plan_view_overlap_with_bridge_clue_stays_possible() -> None:
    from datetime import UTC, datetime

    event = ClosureEvent(
        id="bridge-level-unknown",
        provider="ndw",
        description="Cycleway closure",
        affected_modes=["bike"],
        mode_evidence="explicit",
        grade_separation_evidence=["bridge=yes", "layer=1"],
        starts_at=datetime(2026, 10, 10, tzinfo=UTC),
        ends_at=datetime(2026, 10, 12, tzinfo=UTC),
        timing_quality="precise",
        geometry_quality="precise",
        restriction_type="cycleway closure",
        status="active",
        geometry=Geometry(type="LineString", coordinates=[(5.1, 52.1), (5.1, 52.101)]),
    )
    match = compare_geometry(_route(), event, 20)
    assert match is not None and match.overlap_m > 0
    finding = finding_for(_route(), event, match, True, 20)
    assert finding.classification == "possible_conflict"
    assert any("level relation is unresolved" in item for item in finding.limitations)


@pytest.mark.parametrize(
    "changes",
    [{"restriction_type": "constructionWork"}, {"direction": "positive"}],
)
def test_works_and_unresolved_direction_do_not_establish_blockage(changes: dict) -> None:
    from outdoor_agent.providers.ndw import parse_fixture

    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    event = next(item for item in parse_fixture(fixture).events if item.id == "bike-closure-record")
    event = event.model_copy(update=changes)
    match = compare_geometry(_route(), event, 20)
    assert match is not None and match.overlap_m > 0
    finding = finding_for(_route(), event, match, True, 20)
    assert finding.classification == "possible_conflict"
    assert finding.limitations


def test_transverse_point_intersection_is_possible_not_known() -> None:
    from datetime import UTC, datetime

    event = ClosureEvent(
        id="crossing",
        provider="ndw",
        description="cycleway closure",
        affected_modes=["bike"],
        starts_at=datetime(2026, 10, 10, tzinfo=UTC),
        ends_at=datetime(2026, 10, 12, tzinfo=UTC),
        timing_quality="precise",
        geometry_quality="precise",
        restriction_type="cycleway closure",
        status="active",
        geometry=Geometry(type="LineString", coordinates=[(5.099, 52.1005), (5.101, 52.1005)]),
    )
    match = compare_geometry(_route(), event, 20)
    assert match is not None and match.intersects
    assert match.overlap_m == 0
    finding = finding_for(_route(), event, match, True, 20)
    assert finding.classification == "possible_conflict"


@pytest.mark.asyncio
async def test_bicycle_only_event_is_not_applied_to_foot_route(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    report = await assess_route(
        _settings(tmp_path), _route(Activity.hiking), "2026-10-11", Activity.hiking, fixture
    )
    assert all(item.event_id != "bike-closure-record" for item in report.findings)
    assert report.status == "coverage_unknown"


@pytest.mark.asyncio
async def test_historical_trip_assesses_event_even_if_feed_observed_after_end(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    report = await assess_route(
        _settings(tmp_path), _route(), "2026-10-01", Activity.gravel, fixture
    )
    assert any(item.event_id == "expired-record" for item in report.findings)
    assert report.status == "known_conflict"
    assert report.horizon_status == "historical_not_covered"


@pytest.mark.asyncio
async def test_beyond_observed_horizon_is_coverage_unknown_without_year_magic(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    report = await assess_route(
        _settings(tmp_path), _route(), "2027-01-01", Activity.gravel, fixture
    )
    assert report.status == "coverage_unknown"
    assert report.horizon_status == "beyond_observed_horizon"


@pytest.mark.asyncio
async def test_newer_cancellation_replaces_active_feed_revision(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import timedelta

    import outdoor_agent.services.validation as service
    from outdoor_agent.providers.ndw import FeedFetchResult, FeedSnapshot, parse_fixture

    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    old = parse_fixture(fixture)
    active = next(event for event in old.events if event.id == "bike-closure-record")
    assert active.published_at is not None
    cancelled = active.model_copy(
        update={"status": "cancelled", "published_at": active.published_at + timedelta(hours=1)}
    )
    new = FeedSnapshot(
        events=[cancelled],
        fetched_at=old.fetched_at,
        source_timestamp=old.source_timestamp,
        stale=False,
        from_cache=False,
        source="https://example.com/new",
        warnings=[],
    )

    async def fetch(settings):
        return FeedFetchResult(snapshots=[old, new], errors=[])

    monkeypatch.setattr(service, "fetch_feeds", fetch)
    report = await assess_route(_settings(tmp_path), _route(), "2026-10-11", Activity.gravel)
    assert report.status != "known_conflict"
    assert all(finding.event_id != active.id for finding in report.findings)


@pytest.mark.asyncio
async def test_report_retains_per_source_refresh_and_publication_provenance(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    from outdoor_agent.providers.ndw import FeedFetchResult, FeedSnapshot, parse_fixture

    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    original = parse_fixture(fixture)
    refreshed = datetime.now(UTC) - timedelta(minutes=5)
    published = refreshed - timedelta(hours=2)
    source = FeedSnapshot(
        events=original.events,
        fetched_at=refreshed,
        source_timestamp=published,
        stale=False,
        from_cache=True,
        source="https://feed.example/current",
        warnings=[],
        response_headers={"etag": 'W/"v12"', "last-modified": "Fri, 09 Oct 2026 10:00:00 GMT"},
    )
    report = await assess_route(
        _settings(tmp_path),
        _route(),
        "2026-10-11",
        Activity.gravel,
        feed_result=FeedFetchResult(snapshots=[source], errors=[]),
    )
    freshness = report.source_freshness[0]
    assert freshness.source == source.source
    assert freshness.last_successful_refresh == refreshed
    assert freshness.publication_at == published
    assert freshness.etag == 'W/"v12"'
    assert freshness.last_modified == "Fri, 09 Oct 2026 10:00:00 GMT"
    assert freshness.from_cache is True
    assert freshness.refresh_age_seconds is not None
    assert freshness.publication_age_seconds is not None


@pytest.mark.asyncio
async def test_nationwide_unlocated_events_are_summarized_with_bounded_output(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from outdoor_agent.providers.ndw import FeedFetchResult, FeedSnapshot

    now = datetime.now(UTC)
    events = [
        ClosureEvent(
            id=f"alertc-{index}",
            provider="ndw",
            source_url="https://feed.example/current",
            description=f"Unresolved Alert-C restriction {index}",
            location_reference=f"AlertC method 4; location={index}",
            location_reference_type="AlertCMethod4Linear",
            geometry=None,
            geometry_quality="unresolved",
            starts_at=datetime(2026, 10, 10, tzinfo=UTC),
            ends_at=datetime(2026, 10, 12, tzinfo=UTC),
            timing_quality="precise",
            affected_modes=["bike"],
            mode_evidence="explicit",
            restriction_type="cyclewayClosure",
            status="active",
        )
        for index in range(4_572)
    ]
    snapshot = FeedSnapshot(
        events=events,
        fetched_at=now,
        source_timestamp=now,
        stale=False,
        from_cache=False,
        source="https://feed.example/current",
        warnings=[],
    )
    report = await assess_route(
        _settings(tmp_path),
        _route(),
        "2026-10-11",
        Activity.gravel,
        feed_result=FeedFetchResult(snapshots=[snapshot], errors=[]),
    )
    assert report.status == "coverage_unknown"
    assert report.unknown_coverage is True
    assert report.findings == []
    assert report.events == []
    assert len(report.coverage_summaries) == 1
    summary = report.coverage_summaries[0]
    assert summary.reason == "unresolved_location"
    assert summary.count == 4_572
    assert len(summary.sample_event_ids) == 5
    assert len(summary.digest) == 64
    assert report.finding_limit == 100
    assert len(report.model_dump_json()) < 20_000

    from dataclasses import replace

    timestamp_only = events[-1].model_copy(
        update={"observed_at": now.replace(microsecond=0), "published_at": now}
    )
    timestamp_snapshot = replace(snapshot, events=[*events[:-1], timestamp_only])
    timestamp_report = await assess_route(
        _settings(tmp_path),
        _route(),
        "2026-10-11",
        Activity.gravel,
        feed_result=FeedFetchResult(snapshots=[timestamp_snapshot], errors=[]),
    )
    assert timestamp_report.coverage_summaries[0].digest == summary.digest

    changed = events[-1].model_copy(update={"description": "Changed restriction description"})
    changed_snapshot = replace(snapshot, events=[*events[:-1], changed])
    changed_report = await assess_route(
        _settings(tmp_path),
        _route(),
        "2026-10-11",
        Activity.gravel,
        feed_result=FeedFetchResult(snapshots=[changed_snapshot], errors=[]),
    )
    assert changed_report.coverage_summaries[0].digest != summary.digest


@pytest.mark.asyncio
async def test_summary_overflow_folds_sources_without_dropping_digest_evidence(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from outdoor_agent.providers.ndw import FeedFetchResult, FeedSnapshot

    now = datetime.now(UTC)
    events = [
        ClosureEvent(
            id=f"very-long-event-id-{index}-" + ("x" * 240),
            provider="ndw",
            source_url=f"https://feed.example/source/{index}",
            description=f"Unresolved event {index}",
            geometry=None,
            geometry_quality="unresolved",
            starts_at=datetime(2026, 10, 10, tzinfo=UTC),
            ends_at=datetime(2026, 10, 12, tzinfo=UTC),
            timing_quality="precise",
            affected_modes=["bike"],
            mode_evidence="explicit",
            restriction_type="cyclewayClosure",
            status="active",
        )
        for index in range(25)
    ]
    snapshot = FeedSnapshot(
        events=events,
        fetched_at=now,
        source_timestamp=now,
        stale=False,
        from_cache=False,
        source="https://feed.example/current",
        warnings=[],
    )
    report = await assess_route(
        _settings(tmp_path),
        _route(),
        "2026-10-11",
        Activity.gravel,
        feed_result=FeedFetchResult(snapshots=[snapshot], errors=[]),
    )
    assert len(report.coverage_summaries) <= 12
    assert sum(summary.count for summary in report.coverage_summaries) == len(events)
    overflow = next(
        summary for summary in report.coverage_summaries if summary.source == "other_sources"
    )
    assert overflow.reason == "unresolved_location"
    assert overflow.count > 0
    assert all(
        len(sample_id) <= 160
        for summary in report.coverage_summaries
        for sample_id in summary.sample_event_ids
    )


@pytest.mark.asyncio
async def test_route_specific_findings_are_capped_but_omissions_are_summarized(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from outdoor_agent.providers.ndw import FeedFetchResult, FeedSnapshot

    now = datetime.now(UTC)
    event_geometry = Geometry(type="LineString", coordinates=[(5.1, 52.1), (5.1, 52.101)])
    events = [
        ClosureEvent(
            id=f"bike-{index}",
            provider="ndw",
            source_url="https://feed.example/current",
            description="Cycleway closure",
            geometry=event_geometry,
            geometry_quality="precise",
            starts_at=datetime(2026, 10, 10, tzinfo=UTC),
            ends_at=datetime(2026, 10, 12, tzinfo=UTC),
            timing_quality="precise",
            affected_modes=["bike"],
            mode_evidence="explicit",
            restriction_type="cyclewayClosure",
            status="active",
        )
        for index in range(105)
    ]
    snapshot = FeedSnapshot(
        events=events,
        fetched_at=now,
        source_timestamp=now,
        stale=False,
        from_cache=False,
        source="https://feed.example/current",
        warnings=[],
    )
    report = await assess_route(
        _settings(tmp_path),
        _route(),
        "2026-10-11",
        Activity.gravel,
        feed_result=FeedFetchResult(snapshots=[snapshot], errors=[]),
    )
    assert report.status == "known_conflict"
    assert len(report.findings) == report.finding_limit == 100
    assert len(report.events) == 100
    assert report.unknown_coverage is True
    assert len(report.coverage_summaries) == 1
    summary = report.coverage_summaries[0]
    assert summary.reason == "route_finding_limit"
    assert summary.count == 5
    assert len(summary.sample_event_ids) == 5


@pytest.mark.asyncio
async def test_inside_route_fixture_is_known_conflict(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/closures_inside.xml"
    report = await assess_route(
        _settings(tmp_path), _route(), "2026-10-11", Activity.gravel, fixture
    )
    assert report.status == "known_conflict"
    assert report.source_status == "fixture"
    assert report.fixture_mode is True
    finding = next(item for item in report.findings if item.event_id == "inside-route-record")
    assert finding.classification == "known_conflict"
    assert finding.temporal_overlap is True
    assert finding.mode_overlap is True
    assert finding.overlap_length_m is not None and finding.overlap_length_m > 0
    assert finding.distance_along_route_m is not None
    event = next(item for item in report.events if item.id == finding.event_id)
    assert event.geometry_quality == "precise"
    assert event.timing_quality == "precise"
    assert event.affected_modes == ["bike"]


@pytest.mark.asyncio
async def test_outside_route_fixture_near_is_possible_and_distant_is_ignored(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/closures_outside.xml"
    report = await assess_route(
        _settings(tmp_path), _route(), "2026-10-11", Activity.gravel, fixture
    )
    assert report.status == "possible_conflict"
    assert report.status != "known_conflict"
    assert all(item.classification != "known_conflict" for item in report.findings)
    near = next(item for item in report.findings if item.event_id == "parallel-near-record")
    assert near.classification == "possible_conflict"
    assert near.overlap_length_m is None
    assert near.min_distance_to_route_m is not None
    assert 10 <= near.min_distance_to_route_m <= 20
    assert near.temporal_overlap is True
    assert near.mode_overlap is True
    assert all(item.event_id != "distant-record" for item in report.findings)


@pytest.mark.asyncio
async def test_distant_route_fixture_alone_is_no_known_conflict(tmp_path: Path) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime

    from outdoor_agent.providers.ndw import FeedFetchResult, parse_fixture

    fixture = Path(__file__).parents[1] / "fixtures/closures_outside.xml"
    snapshot = parse_fixture(fixture)
    now = datetime.now(UTC)
    # Fresh observation keeps the report out of the stale-coverage downgrade so the
    # no-evidence outcome of a clearly distant event is asserted directly.
    distant_only = replace(
        snapshot,
        events=[event for event in snapshot.events if event.id == "distant-record"],
        fetched_at=now,
        source_timestamp=now,
    )
    report = await assess_route(
        _settings(tmp_path),
        _route(),
        "2026-10-11",
        Activity.gravel,
        feed_result=FeedFetchResult(snapshots=[distant_only], errors=[]),
    )
    assert report.status == "no_known_conflict"
    assert report.findings == []
    assert report.unknown_coverage is False


@pytest.mark.asyncio
async def test_unresolved_alertc_fixture_is_summarized_not_emitted(tmp_path: Path) -> None:
    from outdoor_agent.providers.ndw import parse_fixture

    fixture = Path(__file__).parents[1] / "fixtures/closures_unresolved.xml"
    parsed = next(
        event for event in parse_fixture(fixture).events if event.id == "alert-c-unresolved-record"
    )
    assert parsed.geometry is None
    assert parsed.geometry_quality == "unresolved"
    assert parsed.location_reference is not None
    assert "AlertCMethod4Linear" in parsed.location_reference

    report = await assess_route(
        _settings(tmp_path), _route(), "2026-10-11", Activity.gravel, fixture
    )
    assert report.status == "coverage_unknown"
    assert report.unknown_coverage is True
    assert report.findings == []
    assert report.events == []
    assert len(report.coverage_summaries) == 1
    summary = report.coverage_summaries[0]
    assert summary.reason == "unresolved_location"
    assert summary.count == 1
    assert summary.sample_event_ids == ["alert-c-unresolved-record"]
    assert len(summary.digest) == 64
    assert any("without route-locatable geometry" in warning for warning in report.warnings)
