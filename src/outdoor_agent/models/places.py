"""Place data and provider enrichment summaries."""

from outdoor_agent.models.routes import Model
from outdoor_agent.providers.base import Place


class NearbyPlace(Place):
    id: str
    name: str
    source_url: str | None = None
    formatted_address: str | None = None
    latitude: float
    longitude: float
    google_maps_uri: str | None = None
    types: list[str]
    source: str = "Google Places"


class PlacesResult(Model):
    places: list[NearbyPlace]
    request_count: int
    limits: dict[str, int | float | str]
    source: str = "Google Places API (New)"
    attribution: str = "Google Maps"
    warnings: list[str]
