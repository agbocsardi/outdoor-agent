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
