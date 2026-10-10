from datetime import UTC, datetime

import httpx
import pytest

from outdoor_agent.config import load_settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.closures import ClosureReport
from outdoor_agent.models.routes import Activity, Geometry, Metric, Route, SourceType
from outdoor_agent.providers.base import RouteDiscoveryProvider, SearchResult
from outdoor_agent.providers.base import SearchCriteria as ProviderSearchCriteria
from outdoor_agent.providers.osm import OSMDiscoveryAdapter, OSMProvider
from outdoor_agent.providers.ridewithgps import RideWithGPSDiscoveryAdapter
from outdoor_agent.services.discovery import (
    RankedRoute,
    RankingExplanation,
    SearchCriteria,
    _bounds,
    search_routes,
)
from outdoor_agent.services.ranking import deduplicate_routes, rank_routes


def make_route(
    route_id: str,
    *,
    name: str = "Route",
    distance: float | None = 20_000,
    loop: bool = True,
) -> Route:
    return Route(
        id=route_id,
        provider="osm",
        provider_route_id=route_id,
        source_url=f"https://example.org/{route_id}",
        source_type=SourceType.official_trail,
        attribution="OSM",
        name=name,
        activity=Activity.hiking,
        geometry=Geometry(type="LineString", coordinates=[(5, 52), (5.01, 52), (5, 52)]),
        start_lon=5,
        start_lat=52,
        end_lon=5,
        end_lat=52,
        distance_m=distance,
        is_loop=loop,
        fetched_at=datetime(2026, 10, 9, tzinfo=UTC),
        geometry_quality="complete",
    )


def test_dedup_is_conservative_and_preserves_alias_urls() -> None:
    a, b, unrelated = make_route("one"), make_route("two"), make_route("three", name="Other")
    generated = a.model_copy(
        update={
            "id": "generated",
            "provider_route_id": "generated",
            "source_type": SourceType.generated,
        }
    )
    results = deduplicate_routes([a, b, unrelated, generated])
    assert len(results) == 3
    assert results[0][0].id == "one"
    assert results[0][1] == ["https://example.org/two"]
    assert [item.provider_route_id for item in results[0][2]] == ["one", "two"]
    assert [item.attribution for item in results[0][2]] == ["OSM", "OSM"]
    assert all(item.source_url for item in results[0][2])
    scored = rank_routes(
        [a, b],
        activity=Activity.hiking,
        center=(5, 52),
        radius_km=25,
        distance_min_m=None,
        distance_max_m=None,
        loop="any",
        weights={"proximity": 1, "activity": 1},
    )[0]
    ranked = RankedRoute(
        route=scored.route,
        ranking=RankingExplanation(
            score=scored.score, factors=scored.factors, explanations=scored.explanations
        ),
        aliases=scored.aliases,
        provenance=scored.provenance,
    )
    assert ranked.model_dump()["provenance"][1]["provider_route_id"] == "two"


def test_ranking_excludes_missing_popularity_from_score_denominator() -> None:
    result = rank_routes(
        [make_route("one")],
        activity=Activity.hiking,
        center=(5, 52),
        radius_km=25,
        distance_min_m=10_000,
        distance_max_m=30_000,
        loop="preferred",
        weights={"distance": 1, "proximity": 1, "activity": 1, "loop": 1, "popularity": 100},
    )
    assert result[0].score == 1
    assert "popularity" not in result[0].factors
    assert any("excluded" in explanation for explanation in result[0].explanations)


def test_source_backed_review_count_contributes_to_popularity_factor() -> None:
    route = make_route("review-count").model_copy(
        update={
            "reviews_count": Metric(
                value=100,
                source_url="https://example.org/routes/1/reviews",
                observed_at=datetime(2026, 10, 9, tzinfo=UTC),
            )
        }
    )
    scored = rank_routes(
        [route],
        activity=Activity.hiking,
        center=(5, 52),
        radius_km=25,
        distance_min_m=None,
        distance_max_m=None,
        loop="any",
        weights={"popularity": 1},
    )[0]
    assert scored.factors["popularity"] == 1
    assert any("source-backed review count" in item for item in scored.explanations)
    assert scored.route.reviews_count is not None
    assert scored.route.reviews_count.source_url == "https://example.org/routes/1/reviews"


def test_popularity_averages_available_metrics_and_unknown_rating_scale_is_explained() -> None:
    route = make_route("metrics").model_copy(
        update={
            "popularity": Metric(
                value=0,
                source_url="https://example.org/popularity",
                observed_at=datetime(2026, 10, 9, tzinfo=UTC),
            ),
            "reviews_count": Metric(
                value=100,
                source_url="https://example.org/reviews",
                observed_at=datetime(2026, 10, 9, tzinfo=UTC),
            ),
            "rating": Metric(
                value=4.8,
                source_url="https://example.org/rating",
                observed_at=datetime(2026, 10, 9, tzinfo=UTC),
            ),
        }
    )
    scored = rank_routes(
        [route],
        activity=Activity.hiking,
        center=(5, 52),
        radius_km=25,
        distance_min_m=None,
        distance_max_m=None,
        loop="any",
        weights={"popularity": 1},
    )[0]
    assert scored.factors["popularity"] == pytest.approx(0.5)
    assert any("were averaged" in item for item in scored.explanations)
    assert any("rating scale is not defined" in item for item in scored.explanations)


def test_requested_distance_range_excludes_routes_with_unknown_length() -> None:
    result = rank_routes(
        [make_route("unknown", distance=None)],
        activity=Activity.hiking,
        center=(5, 52),
        radius_km=25,
        distance_min_m=10_000,
        distance_max_m=30_000,
        loop="any",
        weights={"distance": 1, "proximity": 1, "activity": 1},
    )
    assert result == []


def test_bbox_and_shape_constraints_validate_and_exclude_unknown_shape() -> None:
    criteria = SearchCriteria(bbox=(52.0, 5.0, 52.2, 5.2))
    assert criteria.center == pytest.approx((5.1, 52.1))
    with pytest.raises(ValueError, match="100 km"):
        SearchCriteria(bbox=(51.0, 4.0, 53.0, 6.0))
    with pytest.raises(ValueError, match="conflicts"):
        SearchCriteria(near="Utrecht", loop="required", shape="point_to_point")
    unknown = make_route("unknown-shape").model_copy(update={"is_loop": None})
    assert (
        rank_routes(
            [unknown],
            activity=Activity.hiking,
            center=(5, 52),
            radius_km=25,
            distance_min_m=None,
            distance_max_m=None,
            loop="any",
            weights={},
            shape="loop",
        )
        == []
    )
    point_to_point = make_route("point", loop=False)
    scored = rank_routes(
        [point_to_point],
        activity=Activity.hiking,
        center=(5, 52),
        radius_km=25,
        distance_min_m=None,
        distance_max_m=None,
        loop="preferred",
        weights={"loop": 10, "proximity": 1},
        shape="point_to_point",
    )[0]
    assert "loop" not in scored.factors
    assert (
        rank_routes(
            [unknown],
            activity=Activity.hiking,
            center=(5, 52),
            radius_km=25,
            distance_min_m=None,
            distance_max_m=None,
            loop="any",
            weights={},
            shape="point_to_point",
        )
        == []
    )


def test_typed_osm_adapter_satisfies_discovery_protocol() -> None:
    adapter: RouteDiscoveryProvider = OSMDiscoveryAdapter(OSMProvider(load_settings()))
    assert adapter.declaration.provider == "osm"
    assert ProviderSearchCriteria(bbox=(52, 5, 52.1, 5.1), activity=Activity.hiking)
    assert SearchResult(routes=[], warnings=[]).cached is False


def test_closure_factor_orders_known_conflict_below_scoped_clear_route() -> None:
    clear = make_route("clear", name="Clear")
    conflict = make_route("conflict", name="Conflict").model_copy(update={"start_lon": 5.001})
    clear_report = ClosureReport(
        route_id=clear.id,
        activity="hiking",
        requested_date="2026-10-11",
        status="no_known_conflict",
        source_status="ok",
        unknown_coverage=False,
    )
    conflict_report = ClosureReport(
        route_id=conflict.id,
        activity="hiking",
        requested_date="2026-10-11",
        status="known_conflict",
        source_status="ok",
        unknown_coverage=False,
    )
    ranked = rank_routes(
        [conflict, clear],
        activity=Activity.hiking,
        center=(5, 52),
        radius_km=25,
        distance_min_m=None,
        distance_max_m=None,
        loop="any",
        weights={"proximity": 1, "activity": 1, "closures": 1},
        closure_reports={clear.id: clear_report, conflict.id: conflict_report},
    )
    assert [item.route.id for item in ranked] == [clear.id, conflict.id]
    assert ranked[1].factors["closures"] == 0


def test_unknown_closure_coverage_is_not_scored_as_safe() -> None:
    route = make_route("unknown-closure")
    report = ClosureReport(
        route_id=route.id,
        activity="hiking",
        requested_date="2026-10-11",
        status="coverage_unknown",
        source_status="partial",
        unknown_coverage=True,
    )
    scored = rank_routes(
        [route],
        activity=Activity.hiking,
        center=(5, 52),
        radius_km=25,
        distance_min_m=None,
        distance_max_m=None,
        loop="any",
        weights={"closures": 1, "proximity": 1},
        closure_reports={route.id: report},
    )[0]
    assert "closures" not in scored.factors
    assert any("must not be read as safe" in item for item in scored.explanations)


def test_geodesic_bounds_work_at_high_latitudes_and_report_antimeridian() -> None:
    south, west, north, east = _bounds((5, 60), 25)
    assert 59.7 < south < 60
    assert 60 < north < 60.3
    assert west < 5 < east
    with pytest.raises(OutdoorError) as error:
        _bounds((179.9, 0), 50)
    assert error.value.code == "ANTIMERIDIAN_UNSUPPORTED"


@pytest.mark.asyncio
async def test_live_search_returns_only_osm_and_partial_source_limitations() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"elements": []})

    settings = load_settings()
    settings.overpass_url = "https://overpass-api.de/api/interpreter"
    criteria = SearchCriteria(near="52.09,5.12", radius_km=10, activity=Activity.hiking)
    # search_routes owns its client; test its public composition with an offline cache-free no-result transport
    # via a patched HTTPClient constructor to keep the full flow offline.
    from outdoor_agent.services import discovery

    original = discovery.HTTPClient

    class MockClient(original):
        def __init__(self, settings: object) -> None:
            super().__init__(settings, transport=httpx.MockTransport(handler))

    discovery.HTTPClient = MockClient
    try:
        result = await search_routes(settings, criteria)
    finally:
        discovery.HTTPClient = original
    assert result.routes == []
    assert result.partial is True
    assert result.provider_status[0].provider == "osm"
    assert any("community-route" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_empty_result_reports_filter_counts_and_provider_truncation(monkeypatch) -> None:
    candidates = [
        make_route(f"candidate-{index}", name=f"Candidate {index}").model_copy(
            update={
                "activity": Activity.gravel,
                "geometry": Geometry(
                    type="MultiLineString",
                    coordinates=[[(5.3, 51.7), (5.301, 51.7)], [(5.31, 51.7), (5.311, 51.7)]],
                ),
                "start_lat": 51.7,
                "start_lon": 5.3,
                "distance_m": None,
                "is_loop": None,
                "geometry_quality": "disconnected",
            }
        )
        for index in range(200)
    ]

    async def fake_search(self, criteria: ProviderSearchCriteria) -> SearchResult:
        return SearchResult(
            routes=candidates,
            warnings=[
                "Overpass returned at least 200 relations; additional matches may be omitted."
            ],
        )

    monkeypatch.setattr(OSMDiscoveryAdapter, "search", fake_search)
    result = await search_routes(
        load_settings(),
        SearchCriteria(
            near="51.6978,5.3037",
            activity=Activity.gravel,
            distance_min_m=20_000,
            distance_max_m=35_000,
            loop="required",
            radius_km=20,
        ),
    )
    assert result.routes == []
    assert result.coverage.provider_candidates == 200
    assert result.coverage.unique_candidates == 200
    assert result.coverage.shape_excluded == 200
    assert result.coverage.geometry_not_complete == 200
    assert result.coverage.unknown_distance_with_distance_filter == 200
    assert result.coverage.eligible == 0
    assert result.coverage.provider_truncated is True
    assert any("empty result does not show" in warning for warning in result.warnings)
    assert any("--provider local" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_community_only_fails_typed_without_fixture_fallback() -> None:
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            load_settings(), SearchCriteria(near="Utrecht", source_type="community")
        )
    assert error.value.code == "COMMUNITY_DISCOVERY_UNAVAILABLE"
    assert error.value.exit_code == 5


@pytest.mark.asyncio
async def test_search_trip_date_validation_uses_usage_exit_code() -> None:
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            load_settings(),
            SearchCriteria(near="52.0,5.0", date="not-a-date"),
        )
    assert error.value.code == "INVALID_DATE"
    assert error.value.exit_code == 2


@pytest.mark.asyncio
async def test_unsupported_provider_set_fails_with_exit_five() -> None:
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            load_settings(),
            SearchCriteria(near="Utrecht", providers=["wikiloc", "google"]),
        )
    assert error.value.code == "PROVIDER_UNSUPPORTED"
    assert error.value.exit_code == 5


@pytest.mark.asyncio
async def test_ridewithgps_saved_discovery_is_selectable_with_scope_warning(
    monkeypatch, tmp_path
) -> None:
    from outdoor_agent.config import Settings

    route = make_route("ridewithgps:1").model_copy(
        update={"provider": "ridewithgps", "source_type": SourceType.community}
    )

    async def fake_search(self, criteria: ProviderSearchCriteria) -> SearchResult:
        return SearchResult(routes=[route], warnings=[])

    monkeypatch.setattr(RideWithGPSDiscoveryAdapter, "search", fake_search)
    settings = Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        ride_api_key="synthetic-key",
        ride_auth_token="synthetic-token",
    )
    result = await search_routes(
        settings,
        SearchCriteria(center=(5.0, 52.0), providers=["ridewithgps"], radius_km=20),
    )
    assert [item.route.id for item in result.routes] == ["ridewithgps:1"]
    assert result.provider_status[0].provider == "ridewithgps"
    assert result.provider_status[0].status == "ok"
    assert any("authenticated account" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_ridewithgps_saved_routes_are_not_folded_into_community_source() -> None:
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            load_settings(),
            SearchCriteria(
                center=(5.0, 52.0),
                source_type="community",
                providers=["ridewithgps"],
            ),
        )
    assert error.value.code == "COMMUNITY_DISCOVERY_UNAVAILABLE"
    assert error.value.exit_code == 5


@pytest.mark.asyncio
async def test_empty_ridewithgps_account_propagates_typed_error(monkeypatch, tmp_path) -> None:
    from outdoor_agent.config import Settings
    from outdoor_agent.services import discovery

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "routes": [],
                "meta": {
                    "pagination": {
                        "record_count": 0,
                        "page_count": 0,
                        "page_size": 20,
                        "next_page_url": None,
                    }
                },
            },
        )

    original = discovery.HTTPClient

    class MockClient(original):
        def __init__(self, config: object) -> None:
            super().__init__(config, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(discovery, "HTTPClient", MockClient)
    settings = Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        ride_api_key="synthetic-key",
        ride_auth_token="synthetic-token",
    )
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            settings,
            SearchCriteria(center=(5.0, 52.0), providers=["ridewithgps"], radius_km=20),
        )
    assert error.value.code == "RIDEWITHGPS_NO_SAVED_ROUTES"
    assert error.value.exit_code == 5


@pytest.mark.asyncio
async def test_unsupported_activity_fails_without_empty_success() -> None:
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            load_settings(),
            SearchCriteria(near="52.09,5.12", activity=Activity.other),
        )
    assert error.value.code == "ACTIVITY_UNSUPPORTED"
    assert error.value.exit_code == 5


@pytest.mark.asyncio
async def test_provider_outage_fails_with_exit_four() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    settings = load_settings()
    from outdoor_agent.services import discovery

    original = discovery.HTTPClient

    class MockClient(original):
        def __init__(self, settings: object) -> None:
            super().__init__(settings, transport=httpx.MockTransport(handler))

    discovery.HTTPClient = MockClient
    try:
        with pytest.raises(OutdoorError) as error:
            await search_routes(settings, SearchCriteria(near="52.09,5.12"))
    finally:
        discovery.HTTPClient = original
    assert error.value.code == "PROVIDERS_UNAVAILABLE"
    assert error.value.exit_code == 4


@pytest.mark.asyncio
async def test_dated_search_fetches_closure_feeds_once_for_candidate_batch(monkeypatch) -> None:
    from outdoor_agent.providers.ndw import FeedFetchResult
    from outdoor_agent.services import discovery

    routes = [make_route("first"), make_route("second", name="Other")]
    feeds = FeedFetchResult(snapshots=[], errors=[])
    counts = {"fetch": 0, "assess": 0}

    async def fake_search(self, criteria: ProviderSearchCriteria) -> SearchResult:
        return SearchResult(routes=routes, warnings=[])

    async def fake_fetch(settings):
        counts["fetch"] += 1
        return feeds

    async def fake_assess(settings, route, date, activity, feed_file=None, *, feed_result=None):
        counts["assess"] += 1
        assert feed_result is feeds
        return ClosureReport(
            route_id=route.id,
            activity=activity.value,
            requested_date=date,
            status="coverage_unknown",
            source_status="failed",
            unknown_coverage=True,
            warnings=["test feed unavailable"],
        )

    monkeypatch.setattr(OSMDiscoveryAdapter, "search", fake_search)
    monkeypatch.setattr(discovery, "fetch_feeds", fake_fetch)
    monkeypatch.setattr(discovery, "assess_route", fake_assess)
    result = await search_routes(
        load_settings(),
        SearchCriteria(
            bbox=(51.9, 4.9, 52.1, 5.1),
            date="2026-10-11",
            radius_km=25,
        ),
    )
    assert counts == {"fetch": 1, "assess": 2}
    assert all(item.closure_assessment is not None for item in result.routes)
    assert all(item.closure_assessment.unknown_coverage for item in result.routes)
    assert result.partial is True


@pytest.mark.asyncio
async def test_dated_search_skips_feed_fetch_when_no_candidates_pass_filters(monkeypatch) -> None:
    from outdoor_agent.services import discovery

    far_route = make_route("far").model_copy(update={"start_lon": 20.0})
    counts = {"fetch": 0}

    async def fake_search(self, criteria: ProviderSearchCriteria) -> SearchResult:
        return SearchResult(routes=[far_route], warnings=[])

    async def fake_fetch(settings):
        counts["fetch"] += 1
        raise AssertionError("no eligible candidates should trigger feed I/O")

    monkeypatch.setattr(OSMDiscoveryAdapter, "search", fake_search)
    monkeypatch.setattr(discovery, "fetch_feeds", fake_fetch)
    result = await search_routes(
        load_settings(),
        SearchCriteria(
            bbox=(51.9, 4.9, 52.1, 5.1),
            date="2026-10-11",
            radius_km=25,
        ),
    )
    assert result.routes == []
    assert counts["fetch"] == 0


def _explore_item(route_id: int = 1, **overrides) -> dict:
    base = {
        "type": "route",
        "id": route_id,
        "url": f"/routes/{route_id}",
        "user_id": 17,
        "byline_name": "Ada Rider",
        "name": f"Explore route {route_id}",
        "description": "Public community route",
        "distance": 2000,
        "elevation_gain": 30,
        "first_lat": 52.0,
        "first_lng": 5.0,
        "simplified_polyline": "encoded",
        "track_type": "loop",
        "surface": "gravel",
        "unpaved_pct": 42,
        "activity_types": ["walking:hiking"],
        "likes_count": 5,
        "reviews_count": 2,
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-02-03T04:05:06Z",
        "locality": "Utrecht",
        "country_code": "NL",
    }
    base.update(overrides)
    return base


def _explore_page(results: list[dict], next_page: str | None = None) -> dict:
    return {
        "results": results,
        "total_count": len(results),
        "meta": {"explore": {"next_page_url": next_page, "bounding_box_extended": True}},
    }


def _mock_discovery_http(monkeypatch, handler) -> None:
    from outdoor_agent.services import discovery

    original = discovery.HTTPClient

    class MockClient(original):
        def __init__(self, config: object) -> None:
            super().__init__(config, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(discovery, "HTTPClient", MockClient)


@pytest.mark.asyncio
async def test_ridewithgps_public_community_search_executes_explore(monkeypatch, tmp_path) -> None:
    from outdoor_agent.config import Settings

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/explore.json"
        return httpx.Response(200, json=_explore_page([_explore_item(1)]))

    _mock_discovery_http(monkeypatch, handler)
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    result = await search_routes(
        settings,
        SearchCriteria(
            center=(5.0, 52.0),
            source_type="community",
            providers=["ridewithgps_public"],
            radius_km=20,
        ),
    )
    assert calls == ["/explore.json"]
    assert [item.route.id for item in result.routes] == ["ridewithgps:1"]
    assert result.routes[0].route.source_type == SourceType.community
    assert result.routes[0].route.provider == "ridewithgps_public"
    assert result.provider_status[0].provider == "ridewithgps_public"
    assert any("experimental and degraded" in warning for warning in result.warnings)
    assert any("undocumented website endpoint" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_public_community_warning_suppressed_when_explore_returns_results(
    monkeypatch, tmp_path
) -> None:
    from outdoor_agent.config import Settings

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/explore.json":
            return httpx.Response(200, json=_explore_page([_explore_item(1)]))
        return httpx.Response(200, json={"elements": []})

    _mock_discovery_http(monkeypatch, handler)
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    result = await search_routes(
        settings,
        SearchCriteria(
            center=(5.0, 52.0),
            source_type="all",
            providers=["osm", "ridewithgps_public"],
            radius_km=20,
        ),
    )
    assert [item.route.id for item in result.routes] == ["ridewithgps:1"]
    assert not any(
        "No verified public community-route catalog" in warning for warning in result.warnings
    )


@pytest.mark.asyncio
async def test_osm_is_not_run_under_community_source_type(monkeypatch, tmp_path) -> None:
    from outdoor_agent.config import Settings

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/explore.json":
            return httpx.Response(200, json=_explore_page([_explore_item(1)]))
        raise AssertionError("OSM must not run under --source-type community")

    _mock_discovery_http(monkeypatch, handler)
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    result = await search_routes(
        settings,
        SearchCriteria(
            center=(5.0, 52.0),
            source_type="community",
            providers=["osm", "ridewithgps_public"],
            radius_km=20,
        ),
    )
    assert [item.route.id for item in result.routes] == ["ridewithgps:1"]
    assert all(status.provider != "osm" for status in result.provider_status)
