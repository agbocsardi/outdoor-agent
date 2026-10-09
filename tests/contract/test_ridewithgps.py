"""Documented authenticated read/export contracts; synthetic account data only."""

from pathlib import Path

import httpx
import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.ridewithgps import RideWithGPS


def config(tmp_path: Path, authenticated: bool = True) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        ride_api_key="synthetic-key" if authenticated else None,
        ride_auth_token="synthetic-token" if authenticated else None,
    )


@pytest.mark.asyncio
async def test_missing_credentials_never_call_provider(tmp_path: Path) -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No unauthenticated request should be made")

    async with HTTPClient(config(tmp_path, False), transport=httpx.MockTransport(denied)) as http:
        with pytest.raises(OutdoorError) as error:
            await RideWithGPS(http.settings, http).get("ridewithgps:42")
    assert error.value.exit_code == 3
    assert error.value.code == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_documented_header_auth_and_metadata_provenance(tmp_path: Path) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-rwgps-api-key"] == "synthetic-key"
        assert request.headers["x-rwgps-auth-token"] == "synthetic-token"
        assert not request.url.query
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "route": {
                    "id": 42,
                    "user_id": 17,
                    "name": "Synthetic existing route",
                    "distance": 2000,
                    "elevation_gain": 15,
                    "first_lat": 52,
                    "first_lng": 5,
                    "last_lat": 52.01,
                    "last_lng": 5.01,
                    "activity_types": ["cycling:gravel"],
                    "track_points": [{"x": 5, "y": 52}, {"x": 5.01, "y": 52.01}],
                }
            },
        )

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(reply)) as http:
        route = await RideWithGPS(http.settings, http).get("ridewithgps:42")
    assert route.source_url == "https://ridewithgps.com/routes/42"
    assert route.attribution == "Ride with GPS route creator #17"
    assert route.activity.value == "gravel"
    assert route.distance_m == 2000
    assert route.popularity is None
    assert route.gpx_access == "unverified"
    assert not (tmp_path / "cache/http").exists()
    assert "synthetic-token" not in route.model_dump_json()


@pytest.mark.parametrize("value", ["../1", "ridewithgps:1?x=y", "0", "-1", "1;bad", "1" * 21])
@pytest.mark.asyncio
async def test_route_id_rejected_before_network(tmp_path: Path, value: str) -> None:
    async with HTTPClient(
        config(tmp_path), transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as http:
        with pytest.raises(OutdoorError) as error:
            await RideWithGPS(http.settings, http).get(value)
    assert error.value.code == "INVALID_ROUTE_ID"


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_auth_and_permission_denials_redacted(tmp_path: Path, status: int) -> None:
    async with HTTPClient(
        config(tmp_path),
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text="synthetic-token")),
    ) as http:
        with pytest.raises(OutdoorError) as error:
            await RideWithGPS(http.settings, http).export_bytes("42")
    assert error.value.exit_code == 3
    assert "synthetic-token" not in str(error.value)


@pytest.mark.asyncio
async def test_export_preserves_valid_bytes(tmp_path: Path, example_gpx: Path) -> None:
    payload = example_gpx.read_bytes()
    async with HTTPClient(
        config(tmp_path),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload)),
    ) as http:
        assert await RideWithGPS(http.settings, http).export_bytes("42") == payload


@pytest.mark.asyncio
async def test_invalid_export_and_changed_schema(tmp_path: Path) -> None:
    async with HTTPClient(
        config(tmp_path),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b'{"route":{"id":99}}')
        ),
    ) as http:
        with pytest.raises(OutdoorError) as error:
            await RideWithGPS(http.settings, http).get("42")
        assert error.value.code == "PROVIDER_SCHEMA_INVALID"
        with pytest.raises(OutdoorError) as error:
            await RideWithGPS(http.settings, http).export_bytes("42")
        assert error.value.code == "PROVIDER_GPX_INVALID"


@pytest.mark.asyncio
async def test_auth_probe_uses_documented_current_user_endpoint(tmp_path: Path) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/users/current"
        return httpx.Response(200, json={"user": {"id": 17, "email": "private@example.com"}})

    async with HTTPClient(config(tmp_path), transport=httpx.MockTransport(reply)) as http:
        result = await RideWithGPS(http.settings, http).probe_auth()
    assert result["state"] == "authenticated"
    assert result["account_reference"] != "17"
    assert "private@example.com" not in str(result)
