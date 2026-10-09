from datetime import UTC, datetime
from pathlib import Path

import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import Activity, Geometry, Route, SourceType
from outdoor_agent.providers.base import (
    RouteDiscoveryProvider,
    SearchResult,
)
from outdoor_agent.providers.base import (
    SearchCriteria as ProviderSearchCriteria,
)
from outdoor_agent.providers.local import PROVENANCE_WARNING, LocalCatalogProvider
from outdoor_agent.providers.osm import OSMDiscoveryAdapter
from outdoor_agent.providers.registry import registry
from outdoor_agent.services.discovery import SearchCriteria, search_routes
from outdoor_agent.storage.db import RouteStore


def _route(
    route_id: str,
    *,
    source_type: SourceType = SourceType.community,
    activity: Activity = Activity.hiking,
    points: list[tuple[float, float]] | None = None,
) -> Route:
    geometry = Geometry(
        type="LineString",
        coordinates=points or [(5.0, 52.0), (5.01, 52.0)],
    )
    return Route(
        id=route_id,
        provider="local",
        provider_route_id=route_id,
        source_url=f"https://ridewithgps.com/routes/{route_id.split(':')[-1]}"
        if route_id.split(":")[-1].isdigit()
        else None,
        source_type=source_type,
        attribution="User supplied attribution",
        name=route_id,
        activity=activity,
        geometry=geometry,
        start_lon=geometry.coordinates[0][0],
        start_lat=geometry.coordinates[0][1],
        distance_m=None,
        popularity=None,
        fetched_at=datetime(2026, 10, 9, tzinfo=UTC),
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        offline=True,
    )


def test_local_provider_declares_saved_search_only(tmp_path: Path) -> None:
    declaration = next(item for item in registry() if item.provider == "local")
    assert declaration.capabilities["discover_saved"].state.value == "supported"
    assert declaration.capabilities["discover_public"].state.value == "unsupported"
    provider: RouteDiscoveryProvider = LocalCatalogProvider(_settings(tmp_path))
    assert provider.declaration.provider == "local"


@pytest.mark.asyncio
async def test_local_search_reads_only_saved_community_routes_and_preserves_metadata(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = RouteStore(settings.data_dir)
    community = _route("local:community")
    official = _route("local:official", source_type=SourceType.official_trail)
    generated = _route("local:generated", source_type=SourceType.generated)
    unrelated = _route("local:mtb", activity=Activity.mtb)
    for route in (community, official, generated, unrelated):
        store.save_route(route)

    provider = LocalCatalogProvider(settings, source_type="community")
    result = await provider.search(
        ProviderSearchCriteria(activity=Activity.hiking, bbox=(51.9, 4.9, 52.1, 5.1), limit=20)
    )
    assert [route.id for route in result.routes] == [community.id]
    found = result.routes[0]
    assert found.attribution == community.attribution
    assert found.geometry == community.geometry
    assert found.distance_m is None
    assert found.popularity is None
    assert PROVENANCE_WARNING in found.warnings
    assert result.warnings == []


@pytest.mark.asyncio
async def test_bbox_accepts_intersecting_geometry_even_when_start_is_outside(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    crossing = _route("local:crossing", points=[(4.8, 52.0), (5.2, 52.0)])
    RouteStore(settings.data_dir).save_route(crossing)
    result = await LocalCatalogProvider(settings, source_type="community").search(
        ProviderSearchCriteria(activity=Activity.hiking, bbox=(51.99, 4.99, 52.01, 5.01))
    )
    assert [route.id for route in result.routes] == [crossing.id]


@pytest.mark.asyncio
async def test_empty_local_catalog_is_typed_and_never_scans_fixture_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    (settings.data_dir / "fixture.gpx").write_text("not a route store record")
    with pytest.raises(OutdoorError) as error:
        await LocalCatalogProvider(settings, source_type="community").search(
            ProviderSearchCriteria(activity=Activity.hiking, bbox=(51.9, 4.9, 52.1, 5.1))
        )
    assert error.value.code == "LOCAL_CATALOG_EMPTY"
    assert "--source-type community --attribution" in error.value.message
    assert not (settings.data_dir / "state.sqlite3").exists()


@pytest.mark.asyncio
async def test_community_service_requires_explicit_local_provider(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            settings,
            SearchCriteria(near="52.0,5.0", source_type="community", providers=["osm"]),
        )
    assert error.value.code == "COMMUNITY_DISCOVERY_UNAVAILABLE"
    assert error.value.exit_code == 5


@pytest.mark.asyncio
async def test_local_community_search_is_offline_and_not_marked_partial(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route = _route("local:community")
    RouteStore(settings.data_dir).save_route(route)
    result = await search_routes(
        settings,
        SearchCriteria(
            center=(5.0, 52.0),
            source_type="community",
            providers=["local"],
            radius_km=20,
        ),
    )
    assert [item.route.id for item in result.routes] == [route.id]
    assert result.provider_status[0].provider == "local"
    assert result.provider_status[0].status == "ok"
    assert result.partial is False
    assert PROVENANCE_WARNING in result.routes[0].route.warnings


@pytest.mark.asyncio
async def test_empty_local_catalog_propagates_typed_error_from_service(tmp_path: Path) -> None:
    with pytest.raises(OutdoorError) as error:
        await search_routes(
            _settings(tmp_path),
            SearchCriteria(center=(5.0, 52.0), source_type="community", providers=["local"]),
        )
    assert error.value.code == "LOCAL_CATALOG_EMPTY"


@pytest.mark.asyncio
async def test_mixed_search_keeps_public_gap_warning_and_source_labels(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    community = _route("local:community")
    RouteStore(settings.data_dir).save_route(community)
    official = _route("osm:relation:42", source_type=SourceType.official_trail).model_copy(
        update={"provider": "osm", "source_url": "https://www.openstreetmap.org/relation/42"}
    )

    async def fake_osm_search(self, criteria: ProviderSearchCriteria) -> SearchResult:
        return SearchResult(routes=[official], warnings=[])

    monkeypatch.setattr(OSMDiscoveryAdapter, "search", fake_osm_search)
    result = await search_routes(
        settings,
        SearchCriteria(
            center=(5.0, 52.0),
            source_type="all",
            providers=["osm", "local"],
            radius_km=20,
        ),
    )
    assert {item.route.source_type for item in result.routes} == {
        SourceType.community,
        SourceType.official_trail,
    }
    assert any("No verified public community-route catalog" in item for item in result.warnings)
    assert result.partial is True
