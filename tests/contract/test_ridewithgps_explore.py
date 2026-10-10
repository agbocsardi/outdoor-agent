"""Offline contracts for the experimental, undocumented Ride with GPS Explore adapter.

Every test uses ``httpx.MockTransport``: no live calls, no credentials and no ride CLI.
"""

from pathlib import Path
from typing import Any

import httpx
import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Activity, SourceType
from outdoor_agent.providers import ridewithgps_explore as explore
from outdoor_agent.providers.base import RouteDiscoveryProvider
from outdoor_agent.providers.base import SearchCriteria as ProviderSearchCriteria
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.registry import registry
from outdoor_agent.providers.ridewithgps_explore import (
    RideWithGPSExplore,
    RideWithGPSExploreAdapter,
)

BBOX = (52.0, 5.0, 52.2, 5.2)


def config(tmp_path: Path) -> Settings:
    # Deliberately no ride credentials and no ride CLI path: the adapter must not need them.
    return Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")


def item(route_id: int = 1, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "route",
        "id": route_id,
        "url": f"/routes/{route_id}",
        "user_id": 17,
        "byline_name": "Ada Rider",
        "byline_path": "ada",
        "name": f"Explore route {route_id}",
        "description": "A public community route",
        "distance": 2000,
        "elevation_gain": 30,
        "elevation_loss": 30,
        "first_lat": 52.0,
        "first_lng": 5.0,
        "bounding_box": [
            {"lat": 52.0, "lng": 5.0},
            {"lat": 52.01, "lng": 5.01},
        ],
        "simplified_polyline": "encoded-simplified-polyline",
        "track_type": "loop",
        "surface": "gravel",
        "unpaved_pct": 42,
        "pavement_type": "unpaved",
        "terrain": "rolling",
        "difficulty": "moderate",
        "max_grade": 8.0,
        "activity_types": ["walking:hiking"],
        "likes_count": 5,
        "reviews_count": 2,
        "positive_reviews_count": 1,
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-02-03T04:05:06Z",
        "has_course_points": False,
        "locality": "Utrecht",
        "administrative_area": "Utrecht",
        "country_code": "NL",
        "visibility": "public",
    }
    base.update(overrides)
    return base


def page(results: list[dict[str, Any]], next_page: str | None = None) -> dict[str, Any]:
    return {
        "results": results,
        "total_count": len(results),
        "extras": [],
        "permissions": {},
        "meta": {
            "explore": {
                "bounding_box_extended": True,
                "next_page_url": next_page,
            }
        },
    }


@pytest.fixture(autouse=True)
def fast_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the offline contract fast; production still uses the polite default."""
    monkeypatch.setattr(HTTPClient, "_reserve_request", lambda self, provider, interval: 0.0)


async def run(
    settings: Settings,
    handler: Any,
    criteria: ProviderSearchCriteria,
) -> tuple[Any, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async with HTTPClient(settings, transport=httpx.MockTransport(wrapped)) as http:
        result = await RideWithGPSExplore(settings, http).search(criteria)
    return result, requests


@pytest.mark.asyncio
async def test_request_serialization_uses_plural_selectors_and_meters(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=page([item(1, activity_types=["cycling:gravel"], distance=20_000)])
        )

    criteria = ProviderSearchCriteria(
        bbox=BBOX,
        activity=Activity.gravel,
        distance_min_m=10_000,
        distance_max_m=30_000,
        shape="loop",
        limit=5,
    )
    result, requests = await run(config(tmp_path), handler, criteria)
    assert [route.id for route in result.routes] == ["ridewithgps:1"]
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/explore.json"
    assert dict(request.url.params) == {
        "models": "routes",
        "bounding_box": "52.0,5.0,52.2,5.2",
        "activity_types": "cycling:gravel",
        "length_min": "10000",
        "length_max": "30000",
        "track_types": "loop",
    }
    # Guessed singular parameters must never be sent.
    assert not {"activity_type", "track_type", "surface", "distance_min", "distance_max"} & set(
        request.url.params
    )
    # No credentials, cookies or authenticated headers.
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    assert not any(name.startswith("x-rwgps") for name in request.headers)


@pytest.mark.asyncio
async def test_point_to_point_shape_requests_out_and_back(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([item(1, track_type="out_and_back")]))

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, shape="point_to_point", limit=5
    )
    result, requests = await run(config(tmp_path), handler, criteria)
    assert dict(requests[0].url.params)["track_types"] == "out_and_back"
    assert [route.id for route in result.routes] == ["ridewithgps:1"]


@pytest.mark.asyncio
async def test_filters_apply_before_limit(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=page(
                [
                    item(1, first_lat=10.0, first_lng=10.0),  # outside radius
                    item(2, distance=50_000),  # outside distance range
                    item(3, track_type="out_and_back"),  # not a loop
                    item(4, activity_types=["cycling:gravel"]),  # wrong activity
                    item(5),  # valid
                ]
            ),
        )

    criteria = ProviderSearchCriteria(
        bbox=BBOX,
        activity=Activity.hiking,
        center=(5.0, 52.0),
        radius_km=25,
        distance_min_m=1_000,
        distance_max_m=5_000,
        loop="required",
        limit=1,
    )
    result, _ = await run(config(tmp_path), handler, criteria)
    assert [route.id for route in result.routes] == ["ridewithgps:5"]


@pytest.mark.asyncio
async def test_limit_is_applied_after_filtering(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([item(index) for index in range(1, 51)]))

    criteria = ProviderSearchCriteria(
        bbox=BBOX,
        activity=Activity.hiking,
        center=(5.0, 52.0),
        radius_km=25,
        limit=1,
    )
    result, _ = await run(config(tmp_path), handler, criteria)
    assert [route.id for route in result.routes] == ["ridewithgps:1"]


@pytest.mark.asyncio
async def test_pagination_follows_same_origin_next_pointer(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/explore.json"
        if dict(request.url.params).get("page") == "2":
            return httpx.Response(200, json=page([item(2)]))
        return httpx.Response(
            200,
            json=page(
                [item(1)],
                "https://ridewithgps.com/explore.json?models=routes&bounding_box=52.0,5.0,52.2,5.2&page=2",
            ),
        )

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=10
    )
    result, requests = await run(config(tmp_path), handler, criteria)
    assert [route.id for route in result.routes] == ["ridewithgps:1", "ridewithgps:2"]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_pagination_follows_relative_cursor_pointer(tmp_path: Path) -> None:
    # Live endpoint shape: a relative /explore.json pointer with offset/next_assets/sort_by.
    pointer = (
        "/explore.json?activity_types=cycling%3Agravel&bounding_box=52.0%2C5.0%2C52.2%2C5.2"
        "&length_max=35000&length_min=20000&models=routes"
        "&next_assets=Route-2&offset=30&sort_by&track_types=loop"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if dict(request.url.params).get("offset") == "30":
            return httpx.Response(200, json=page([item(2)]))
        return httpx.Response(200, json=page([item(1)], pointer))

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=10
    )
    result, requests = await run(config(tmp_path), handler, criteria)
    assert [route.id for route in result.routes] == ["ridewithgps:1", "ridewithgps:2"]
    assert len(requests) == 2
    assert requests[1].url.host == "ridewithgps.com"


@pytest.mark.asyncio
async def test_pagination_cycle_is_bounded_and_warns(tmp_path: Path) -> None:
    page_two = "https://ridewithgps.com/explore.json?models=routes&page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if dict(request.url.params).get("page") == "2":
            return httpx.Response(200, json=page([item(2)], page_two))
        return httpx.Response(200, json=page([item(1)], page_two))

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=10
    )
    result, _ = await run(config(tmp_path), handler, criteria)
    assert [route.id for route in result.routes] == ["ridewithgps:1", "ridewithgps:2"]
    assert any("safety bound" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_page_bound_with_pointer_present_truncates_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(explore, "_MAX_PAGES", 2)
    next_pointer = "https://ridewithgps.com/explore.json?models=routes&page=99"

    def handler(request: httpx.Request) -> httpx.Response:
        page_number = dict(request.url.params).get("page", "1")
        return httpx.Response(200, json=page([item(int(page_number))], next_pointer))

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=10
    )
    result, requests = await run(config(tmp_path), handler, criteria)
    assert len(requests) == 2
    assert len(result.routes) == 2
    assert any("additional matches may be omitted" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "pointer",
    [
        "https://evil.example/explore.json?page=2",
        "https://ridewithgps.com/api/v1/routes.json?page=2",
        "https://ridewithgps.com/explore.json?page=2&token=secret",
        "https://ridewithgps.com/explore.json",
        "http://ridewithgps.com/explore.json?page=2",
        "https://ridewithgps.com:8443/explore.json?page=2",
    ],
)
@pytest.mark.asyncio
async def test_rejects_foreign_or_disallowed_pagination_pointer(
    tmp_path: Path, pointer: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([item(1)], pointer))

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=10
    )
    with pytest.raises(OutdoorError) as error:
        await run(config(tmp_path), handler, criteria)
    assert error.value.code == "PROVIDER_SCHEMA_INVALID"
    assert error.value.provider == "ridewithgps_public"
    assert error.value.exit_code == 4


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": "not-a-list", "total_count": 1},
        {"results": [item(1)]},
        {"results": [{"type": "route", "id": "bad", "url": "/routes/1"}], "total_count": 1},
        {"results": [item(1, url="/routes/2")], "total_count": 1},
        {"results": [item(1, type="segment")], "total_count": 1},
        {"results": [], "total_count": "many"},
    ],
)
@pytest.mark.asyncio
async def test_malformed_body_is_schema_invalid(tmp_path: Path, payload: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=10
    )
    with pytest.raises(OutdoorError) as error:
        await run(config(tmp_path), handler, criteria)
    assert error.value.code == "PROVIDER_SCHEMA_INVALID"


@pytest.mark.asyncio
async def test_rate_limit_is_typed_and_redacted(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    criteria = ProviderSearchCriteria(bbox=BBOX, activity=Activity.hiking, limit=5)
    with pytest.raises(OutdoorError) as error:
        await run(config(tmp_path), handler, criteria)
    assert error.value.code == "PROVIDER_RATE_LIMITED"
    assert error.value.provider == "ridewithgps_public"
    assert error.value.retryable is True
    assert "429" not in error.value.message


@pytest.mark.asyncio
async def test_oversize_payload_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explore, "_MAX_BYTES", 64)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    criteria = ProviderSearchCriteria(bbox=BBOX, activity=Activity.hiking, limit=5)
    with pytest.raises(OutdoorError) as error:
        await run(config(tmp_path), handler, criteria)
    assert error.value.code == "PROVIDER_PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_wall_time_budget_is_typed_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(explore, "_TOTAL_BUDGET_SECONDS", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=page([item(1)]))

    criteria = ProviderSearchCriteria(bbox=BBOX, activity=Activity.hiking, limit=5)
    with pytest.raises(OutdoorError) as error:
        await run(config(tmp_path), handler, criteria)
    assert error.value.code == "PROVIDER_TIMEOUT"
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_route_provenance_geometry_and_metrics(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([item(7)]))

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=5
    )
    result, _ = await run(config(tmp_path), handler, criteria)
    route = result.routes[0]
    assert route.id == "ridewithgps:7"
    assert route.provider == "ridewithgps"
    assert route.provider_route_id == "7"
    # The canonical provider must resolve the existing authenticated detail/export
    # factories even though discovery came from the ridewithgps_public source.
    from outdoor_agent.services.delivery import (
        DETAIL_PROVIDER_FACTORIES,
        EXPORT_PROVIDER_FACTORIES,
    )

    assert route.provider in DETAIL_PROVIDER_FACTORIES
    assert route.provider in EXPORT_PROVIDER_FACTORIES
    assert route.source_url == "https://ridewithgps.com/routes/7"
    assert route.source_type == SourceType.community
    assert "Ada Rider" in route.attribution
    assert route.geometry is None
    assert route.geometry_quality == "unknown"
    assert any("navigation geometry" in warning for warning in route.warnings)
    assert route.raw_metadata["unpaved_pct"] == 42
    assert route.raw_metadata["surface"] == "gravel"
    assert route.raw_metadata["track_type"] == "loop"
    assert route.reviews_count is not None
    assert route.reviews_count.value == 2
    assert route.reviews_count.source_url == route.source_url
    assert route.reviews_count.observed_at == route.fetched_at
    assert route.popularity is not None
    assert route.popularity.value == 5


@pytest.mark.asyncio
async def test_missing_metrics_are_never_fabricated(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([item(1, likes_count=None, reviews_count=None)]))

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=5
    )
    result, _ = await run(config(tmp_path), handler, criteria)
    route = result.routes[0]
    assert route.popularity is None
    assert route.reviews_count is None


@pytest.mark.asyncio
async def test_unavailable_author_attribution_is_explicit(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([item(1, byline_name=None)]))

    criteria = ProviderSearchCriteria(
        bbox=BBOX, activity=Activity.hiking, center=(5.0, 52.0), radius_km=25, limit=5
    )
    result, _ = await run(config(tmp_path), handler, criteria)
    assert "attribution unavailable" in result.routes[0].attribution


@pytest.mark.asyncio
async def test_adapter_satisfies_discovery_protocol(tmp_path: Path) -> None:
    settings = config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([item(1)]))

    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as http:
        adapter: RouteDiscoveryProvider = RideWithGPSExploreAdapter(
            RideWithGPSExplore(settings, http)
        )
        assert adapter.declaration.provider == "ridewithgps_public"
        assert adapter.declaration.auth_required is False
        result = await adapter.search(
            ProviderSearchCriteria(bbox=BBOX, activity=Activity.hiking, limit=5)
        )
    assert result.routes


def test_registry_declares_experimental_degraded_public_discovery() -> None:
    declaration = next(item for item in registry() if item.provider == "ridewithgps_public")
    assert declaration.implemented is True
    assert declaration.auth_required is False
    capability = declaration.capabilities["discover_public"]
    assert capability.state.value == "degraded"
    assert capability.checked_at is not None
    assert capability.checked_at.date().isoformat() == "2026-10-10"
    assert "https://ridewithgps.com/explore.json" in capability.evidence
    assert "https://ridewithgps.com/robots.txt" in capability.evidence
    notes = capability.notes.casefold()
    for phrase in ("undocumented", "not the documented api", "no credentials", "unverified"):
        assert phrase in notes
    # The authenticated saved-route provider must be untouched.
    saved = next(item for item in registry() if item.provider == "ridewithgps")
    assert saved.capabilities["discover_public"].state.value == "unsupported"
    assert saved.capabilities["discover_saved"].state.value == "supported"
