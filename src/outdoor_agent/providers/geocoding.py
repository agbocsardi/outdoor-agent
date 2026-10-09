"""Nominatim geocoding with a local coordinate shortcut and bounded caching."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Coordinate
from outdoor_agent.providers.http import HTTPClient

if TYPE_CHECKING:
    from outdoor_agent.config import Settings

_COORDINATES = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$")


async def geocode(
    settings: Settings,
    near: str,
    *,
    http: HTTPClient | None = None,
) -> Coordinate:
    """Return WGS84 ``(longitude, latitude)`` for a place or ``lat,lon`` string.

    Coordinate inputs never generate a network request. Public Nominatim is
    queried at most once per second and responses are cached for 30 days.
    """
    match = _COORDINATES.fullmatch(near)
    if match:
        latitude, longitude = map(float, match.groups())
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise OutdoorError(
                "INVALID_LOCATION", "Coordinates must be valid latitude,longitude values."
            )
        return longitude, latitude
    if not near.strip():
        raise OutdoorError("INVALID_LOCATION", "A place name or latitude,longitude is required.")
    if http is None:
        async with HTTPClient(settings) as client:
            return await geocode(settings, near, http=client)
    result = await http.fetch(
        "GET",
        settings.nominatim_url,
        provider="nominatim",
        params={"q": near.strip(), "format": "jsonv2", "limit": 1},
        ttl=30 * 24 * 60 * 60,
        max_bytes=256_000,
        min_interval=1.0,
    )
    import json

    try:
        places = json.loads(result.content)
        if not isinstance(places, list) or not places:
            raise ValueError("empty result")
        latitude = float(places[0]["lat"])
        longitude = float(places[0]["lon"])
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("coordinate out of range")
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise OutdoorError(
            "LOCATION_NOT_FOUND",
            "The location could not be resolved by Nominatim.",
            4,
            provider="nominatim",
        ) from exc
    return longitude, latitude
