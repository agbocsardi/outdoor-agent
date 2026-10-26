"""Provider-boundary contract tests for place enrichment orchestration."""

import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.places import PlacesResult
from outdoor_agent.models.routes import Geometry, Route
from outdoor_agent.providers.base import Place as PlaceModel
from outdoor_agent.providers.base import PlaceEnrichmentProvider
from outdoor_agent.providers.registry import Provider, registry
from outdoor_agent.services import places as places_service


class FakePlacesProvider:
    """Structural implementation of PlaceEnrichmentProvider for dispatch testing."""

    declaration: Provider
    last_result: PlacesResult | None

    def __init__(self, declaration: Provider) -> None:
        self.declaration = declaration
        self.last_result = PlacesResult(places=[], request_count=0, limits={}, warnings=["fixture"])
        self.received: tuple[Geometry, str] | None = None

    async def near_route(self, geometry: Geometry, kind: str) -> list[PlaceModel]:
        self.received = (geometry, kind)
        return []


def make_route() -> Route:
    return Route(
        id="fixture:route",
        provider="fixture",
        provider_route_id="route",
        attribution="Fixture",
        name="Fixture",
        geometry=Geometry(type="LineString", coordinates=[(4.9, 52.3), (4.91, 52.3)]),
    )


@pytest.mark.asyncio
async def test_service_dispatches_through_structural_provider_protocol(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    declaration = next(item for item in registry() if item.provider == "google").model_copy(
        update={"provider": "fixture"}
    )
    fake = FakePlacesProvider(declaration)
    typed_provider: PlaceEnrichmentProvider = fake
    assert typed_provider is fake
    monkeypatch.setitem(
        places_service.PLACE_PROVIDERS,
        "fixture",
        lambda config, client: fake,
    )

    result = await places_service.enrich_places(settings, make_route(), "sample", "fixture")

    assert result.warnings == ["fixture"]
    assert fake.received == (make_route().geometry, "sample")


@pytest.mark.asyncio
async def test_unknown_provider_is_a_typed_capability_error(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    with pytest.raises(OutdoorError) as error:
        await places_service.enrich_places(settings, make_route(), "park", "unknown")
    assert error.value.code == "PROVIDER_UNSUPPORTED"
