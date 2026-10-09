"""Google Places contract coverage with an injected transport; no credentialed calls."""

import json
from typing import Any

import httpx
import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Geometry, Route
from outdoor_agent.providers.google_places import ENDPOINT, FIELD_MASK
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.services.places import enrich_places


def settings(tmp_path: Any, **kwargs: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "cache_dir": tmp_path / "cache",
        "google_enabled": True,
        "max_google_requests_per_run": 1,
    }
    values.update(kwargs)
    return Settings(**values)


def route() -> Route:
    return Route(
        id="local:fixture",
        provider="local",
        provider_route_id="fixture",
        attribution="User supplied GPX",
        name="Fixture",
        geometry=Geometry(type="LineString", coordinates=[(4.9, 52.3), (4.901, 52.3)]),
    )


def place(place_id: str = "places/one") -> dict[str, Any]:
    return {
        "id": place_id,
        "displayName": {"text": "Canal viewpoint"},
        "formattedAddress": "Amsterdam",
        "location": {"latitude": 52.3, "longitude": 4.9005},
        "googleMapsUri": "https://maps.google.com/?cid=123",
        "types": ["tourist_attraction"],
        "rating": 5,
        "reviews": ["must not be requested"],
    }


@pytest.mark.asyncio
async def test_disabled_missing_key_and_category_fail_before_io(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    disabled = settings(tmp_path, google_enabled=False)
    with pytest.raises(OutdoorError) as error:
        await enrich_places(disabled, route(), "park")
    assert error.value.code == "GOOGLE_DISABLED"
    with pytest.raises(OutdoorError) as error:
        await enrich_places(settings(tmp_path), route(), "park")
    assert error.value.code == "GOOGLE_API_KEY_MISSING"
    route_without_geometry = route().model_copy(update={"geometry": None})
    with pytest.raises(OutdoorError) as error:
        await enrich_places(settings(tmp_path), route_without_geometry, "park")
    assert error.value.code == "ROUTE_GEOMETRY_REQUIRED"
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "NEVER-ECHO-THIS-KEY")
    with pytest.raises(OutdoorError) as error:
        await enrich_places(settings(tmp_path), route(), "coffee_shop")
    assert error.value.code == "GOOGLE_CATEGORY_UNSUPPORTED"
    assert "NEVER-ECHO-THIS-KEY" not in error.value.message


@pytest.mark.asyncio
async def test_search_uses_narrow_mask_budget_and_redacted_output(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "SECRET-KEY")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        invalid_coordinates = place("places/invalid-coordinates")
        invalid_coordinates["location"] = {"latitude": 91, "longitude": 4.9}
        return httpx.Response(200, json={"places": [place(), invalid_coordinates]})

    client = HTTPClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    fetch_attempts: list[int] = []
    original_fetch = client.fetch

    async def observed_fetch(*args: Any, **kwargs: Any) -> Any:
        fetch_attempts.append(kwargs["attempts"])
        return await original_fetch(*args, **kwargs)

    client.fetch = observed_fetch  # type: ignore[method-assign]
    async with client:
        result = await enrich_places(settings(tmp_path), route(), "viewpoint", http_client=client)
    assert len(requests) == result.request_count == 1
    assert requests[0].url == ENDPOINT
    assert requests[0].headers["X-Goog-Api-Key"] == "SECRET-KEY"
    assert requests[0].headers["X-Goog-FieldMask"] == FIELD_MASK
    assert fetch_attempts == [1]
    body = json.loads(requests[0].content)
    assert body["includedTypes"] == ["tourist_attraction"]
    assert body["locationRestriction"]["circle"]["radius"] == 500
    assert len(result.places) == 1
    assert result.places[0].name == "Canal viewpoint"
    output = result.model_dump_json()
    assert "SECRET-KEY" not in output
    assert "rating" not in output
    assert "reviews" not in output
    assert any("no stable viewpoint type" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_budget_caps_requests_and_deduplicates_places(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"places": [place()]})

    route_with_long_geometry = route().model_copy(
        update={"geometry": Geometry(type="LineString", coordinates=[(4.9, 52.3), (4.908, 52.3)])}
    )
    config = settings(tmp_path, max_google_requests_per_run=2)
    client = HTTPClient(config, transport=httpx.MockTransport(handler))
    async with client:
        result = await enrich_places(config, route_with_long_geometry, "park", http_client=client)
    assert calls == result.request_count == 2
    assert len(result.places) == 1


@pytest.mark.asyncio
async def test_offline_and_permission_errors_make_no_network_and_are_partial(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    config = settings(tmp_path, offline=True)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"places": [place()]})

    client = HTTPClient(config, transport=httpx.MockTransport(handler))
    async with client:
        result = await enrich_places(config, route(), "park", http_client=client)
    assert called is False
    assert result.places == []
    assert result.request_count == 0
    assert "offline mode" in result.warnings[0]

    permission_config = settings(tmp_path)
    permission_calls = 0

    def forbidden(request: httpx.Request) -> httpx.Response:
        nonlocal permission_calls
        permission_calls += 1
        return httpx.Response(403, json={"error": {"message": "SECRET-KEY denied"}})

    permission_client = HTTPClient(permission_config, transport=httpx.MockTransport(forbidden))
    async with permission_client:
        partial = await enrich_places(
            permission_config, route(), "park", http_client=permission_client
        )
    assert permission_calls == 1
    assert partial.places == []
    assert "PERMISSION_DENIED" in partial.warnings[0]
    assert "SECRET-KEY" not in " ".join(partial.warnings)


@pytest.mark.asyncio
async def test_zero_budget_is_a_typed_error(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    with pytest.raises(OutdoorError) as error:
        await enrich_places(settings(tmp_path, max_google_requests_per_run=0), route(), "park")
    assert error.value.code == "REQUEST_BUDGET_EXHAUSTED"
