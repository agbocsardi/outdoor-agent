from pathlib import Path

import httpx
import pytest

from outdoor_agent.config import load_settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.providers.geocoding import geocode
from outdoor_agent.providers.http import HTTPClient


@pytest.mark.asyncio
async def test_coordinate_input_is_local_and_lat_lon_order(tmp_path: Path) -> None:
    settings = load_settings()
    settings.offline = True
    assert await geocode(settings, "52.09, 5.12") == (5.12, 52.09)
    with pytest.raises(OutdoorError, match="Coordinates"):
        await geocode(settings, "95,5")


@pytest.mark.asyncio
async def test_place_name_uses_cached_identified_nominatim_request() -> None:
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=[{"lat": "52.09", "lon": "5.12"}])

    settings = load_settings()
    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        location = await geocode(settings, "Utrecht, Netherlands", http=client)
    assert location == (5.12, 52.09)
    assert observed[0].url.params["q"] == "Utrecht, Netherlands"
    assert observed[0].headers["user-agent"] == settings.http_user_agent


@pytest.mark.asyncio
async def test_empty_geocoder_result_is_typed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    settings = load_settings()
    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OutdoorError) as error:
            await geocode(settings, "Nowhere", http=client)
    assert error.value.code == "LOCATION_NOT_FOUND"
