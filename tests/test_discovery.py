from datetime import UTC, datetime

import httpx
import pytest

from outdoor_agent.config import load_settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Activity, Geometry, Route, SourceType
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
async def test_community_only_fails_typed_without_fixture_fallback() -> None:
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            load_settings(), SearchCriteria(near="Utrecht", source_type="community")
        )
    assert error.value.code == "COMMUNITY_DISCOVERY_UNAVAILABLE"
    assert error.value.exit_code == 5


@pytest.mark.asyncio
async def test_unsupported_provider_set_fails_with_exit_five() -> None:
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            load_settings(),
            SearchCriteria(near="Utrecht", providers=["ridewithgps", "wikiloc"]),
        )
    assert error.value.code == "PROVIDER_UNSUPPORTED"
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
