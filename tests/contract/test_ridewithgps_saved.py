"""Offline contracts for authenticated saved-route discovery and public URL references."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Activity
from outdoor_agent.providers.base import RouteDiscoveryProvider
from outdoor_agent.providers.base import SearchCriteria as ProviderSearchCriteria
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.registry import registry
from outdoor_agent.providers.ridewithgps import (
    RideWithGPS,
    RideWithGPSDiscoveryAdapter,
    canonical_route_reference,
    numeric_route_id,
)


def config(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        ride_api_key="synthetic-key",
        ride_auth_token="synthetic-token",
    )


def item(route_id: int, activity: str = "cycling:gravel") -> dict[str, Any]:
    return {
        "id": route_id,
        "user_id": 17,
        "name": f"Saved route {route_id}",
        "distance": 2000,
        "elevation_gain": 15,
        "first_lat": 52,
        "first_lng": 5,
        "last_lat": 52.01,
        "last_lng": 5.01,
        "activity_types": [activity],
        "track_points": [{"x": 5, "y": 52}, {"x": 5.01, "y": 52.01}],
    }


def page(routes: list[dict[str, Any]], next_page: str | None) -> dict[str, Any]:
    return {
        "routes": routes,
        "meta": {
            "pagination": {
                "record_count": len(routes),
                "page_count": 1,
                "page_size": 20,
                "next_page_url": next_page,
            }
        },
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://ridewithgps.com/routes/42", "ridewithgps:42"),
        ("https://www.ridewithgps.com/routes/42", "ridewithgps:42"),
        ("https://ridewithgps.com/routes/1", "ridewithgps:1"),
        ("ridewithgps:42", "ridewithgps:42"),
    ],
)
def test_canonical_route_reference_accepts_exact_public_urls(value: str, expected: str) -> None:
    assert canonical_route_reference(value) == expected
    if value.startswith("https://"):
        assert numeric_route_id(value) == expected.split(":", 1)[1]


@pytest.mark.parametrize(
    "value",
    [
        "http://ridewithgps.com/routes/42",
        "https://ridewithgps.example/routes/42",
        "https://evil.example/routes/42",
        "https://ridewithgps.com/routes/42?page=2",
        "https://ridewithgps.com/routes/42#fragment",
        "https://user:pass@ridewithgps.com/routes/42",
        "https://ridewithgps.com:8443/routes/42",
        "https://ridewithgps.com/route/42",
        "https://ridewithgps.com/routes/42/extra",
        "https://ridewithgps.com/routes/0",
        "https://ridewithgps.com/routes/abc",
        "https://ridewithgps.com/routes/42?x=y#z",
        "ridewithgps:42",
    ],
)
def test_canonical_route_reference_leaves_other_values_unchanged(value: str) -> None:
    assert canonical_route_reference(value) == value


def test_numeric_route_id_rejects_non_route_urls() -> None:
    with pytest.raises(OutdoorError) as error:
        numeric_route_id("https://ridewithgps.com/routes/42?page=2")
    assert error.value.code == "INVALID_ROUTE_ID"


@pytest.mark.asyncio
async def test_saved_routes_follow_pagination_and_bound_page_size(tmp_path: Path) -> None:
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/routes.json"
        params = dict(request.url.params)
        queries.append(params)
        if params["page"] == "1":
            return httpx.Response(
                200,
                json=page(
                    [item(1), item(2)],
                    "https://ridewithgps.com/api/v1/routes.json?page=2&page_size=20",
                ),
            )
        return httpx.Response(200, json=page([item(3)], None))

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(handler)) as http:
        routes = await RideWithGPS(http.settings, http).list_saved_routes(limit=10)
    assert [route.id for route in routes] == [
        "ridewithgps:1",
        "ridewithgps:2",
        "ridewithgps:3",
    ]
    assert [query["page"] for query in queries] == ["1", "2"]
    assert all(20 <= int(query["page_size"]) <= 200 for query in queries)


@pytest.mark.asyncio
async def test_saved_routes_respect_limit_without_extra_pages(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=page(
                [item(index) for index in range(1, 21)],
                "https://ridewithgps.com/api/v1/routes.json?page=2&page_size=20",
            ),
        )

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(handler)) as http:
        routes = await RideWithGPS(http.settings, http).list_saved_routes(limit=5)
    assert len(routes) == 5
    assert calls == 1
    assert not any("page or record bound" in warning for warning in routes[0].warnings)


@pytest.mark.asyncio
async def test_saved_routes_activity_filter_is_client_side(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=page([item(1, "cycling:mountain"), item(2, "cycling:road")], None),
        )

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(handler)) as http:
        provider = RideWithGPS(http.settings, http)
        mtb = await provider.list_saved_routes(limit=10, activity=Activity.mtb)
        none = await provider.list_saved_routes(limit=10, activity=Activity.hiking)
    assert [route.id for route in mtb] == ["ridewithgps:1"]
    assert none == []


@pytest.mark.asyncio
async def test_saved_routes_truncation_warns_at_page_bound(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page_number = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=page(
                [item(page_number)],
                f"https://ridewithgps.com/api/v1/routes.json?page={page_number + 1}&page_size=200",
            ),
        )

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(handler)) as http:
        routes = await RideWithGPS(http.settings, http).list_saved_routes(limit=200)
    assert len(routes) == 20
    assert all(any("page or record bound" in warning for warning in r.warnings) for r in routes)


@pytest.mark.parametrize(
    "pagination",
    [
        {"next_page_url": "https://evil.example/api/v1/routes.json?page=2"},
        {"next_page_url": "https://ridewithgps.com/api/v1/other.json?page=2"},
        {"next_page_url": "https://ridewithgps.com/api/v1/routes.json?page=zero"},
        {"next_page_url": "https://ridewithgps.com/api/v1/routes.json?page=0"},
        {"next_page_url": "https://ridewithgps.com/api/v1/routes.json"},
    ],
)
async def test_saved_routes_reject_malformed_pagination(
    tmp_path: Path, pagination: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"routes": [item(1)], "meta": {"pagination": pagination}})

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OutdoorError) as error:
            await RideWithGPS(http.settings, http).list_saved_routes(limit=10)
    assert error.value.code == "PROVIDER_SCHEMA_INVALID"


@pytest.mark.parametrize("payload", [{"routes": [item(1)]}, {"routes": []}, {"meta": {}}])
async def test_saved_routes_reject_missing_pagination(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OutdoorError) as error:
            await RideWithGPS(http.settings, http).list_saved_routes(limit=10)
    assert error.value.code == "PROVIDER_SCHEMA_INVALID"


@pytest.mark.asyncio
async def test_empty_saved_routes_is_typed_and_never_empty_success(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([], None))

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OutdoorError) as error:
            await RideWithGPS(http.settings, http).list_saved_routes(limit=10)
    assert error.value.code == "RIDEWITHGPS_NO_SAVED_ROUTES"
    assert error.value.exit_code == 5
    assert error.value.provider == "ridewithgps"
    assert any("local" in suggestion for suggestion in error.value.suggestions)


@pytest.mark.asyncio
async def test_saved_route_adapter_satisfies_discovery_protocol_and_filters_bbox(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        inside = item(1)
        outside = {**item(2), "first_lat": 10, "first_lng": 10}
        outside["track_points"] = [{"x": 10, "y": 10}, {"x": 10.01, "y": 10.01}]
        outside["last_lat"], outside["last_lng"] = 10.01, 10.01
        return httpx.Response(200, json=page([inside, outside], None))

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(handler)) as http:
        provider = RideWithGPS(http.settings, http)
        adapter: RouteDiscoveryProvider = RideWithGPSDiscoveryAdapter(provider)
        assert adapter.declaration.provider == "ridewithgps"
        result = await adapter.search(
            ProviderSearchCriteria(activity=Activity.gravel, bbox=(51.9, 4.9, 52.1, 5.1), limit=10)
        )
    assert [route.id for route in result.routes] == ["ridewithgps:1"]


def test_registry_declares_saved_discovery_but_not_public() -> None:
    declaration = next(item for item in registry() if item.provider == "ridewithgps")
    assert declaration.capabilities["discover_public"].state.value == "unsupported"
    saved = declaration.capabilities["discover_saved"]
    assert saved.state.value == "supported"
    assert saved.checked_at is not None
    assert any("ridewithgps.com/api/v1/doc" in entry for entry in saved.evidence)
    assert any("listRoutes" in entry for entry in saved.evidence)
    assert "authenticated user" in saved.notes
