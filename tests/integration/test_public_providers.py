"""Explicitly opted-in public reads; no account credentials or paid requests."""

import os

import pytest

from outdoor_agent.config import load_settings
from outdoor_agent.geo.export import route_gpx
from outdoor_agent.geo.gpx import inspect_gpx
from outdoor_agent.models.routes import Activity
from outdoor_agent.providers.geocoding import geocode
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.osm import OSMProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("OUTDOOR_AGENT_LIVE_TESTS") != "1",
        reason="Set OUTDOOR_AGENT_LIVE_TESTS=1 to opt into public provider reads.",
    ),
]


@pytest.mark.asyncio
async def test_public_geocoding_discovery_and_attributed_gpx() -> None:
    settings = load_settings()
    center = await geocode(settings, "Utrecht, Netherlands")
    assert 5 < center[0] < 5.3 and 52 < center[1] < 52.2
    async with HTTPClient(settings) as http:
        routes = await OSMProvider(settings, http).search(
            (52.08, 5.11, 52.10, 5.13), activity=Activity.hiking, limit=3
        )
    assert routes
    route = next(route for route in routes if route.geometry is not None)
    assert route.id.startswith("osm:relation:")
    assert route.source_type.value == "official_trail"
    assert route.source_url == f"https://www.openstreetmap.org/relation/{route.provider_route_id}"
    assert "OpenStreetMap contributors" in route.attribution
    inspection = inspect_gpx(route_gpx(route))
    assert inspection.point_count >= 2
    assert inspection.route.source_url == route.source_url
