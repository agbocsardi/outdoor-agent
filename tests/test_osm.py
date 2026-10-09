import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from outdoor_agent.config import load_settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.export import route_gpx
from outdoor_agent.models.routes import Activity
from outdoor_agent.providers.base import RouteDetailProvider, RouteExportProvider
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.osm import OSMProvider, reconstruct_members, relation_to_route

FIXTURE = Path(__file__).parent / "fixtures/osm-relations.json"


def test_reconstructs_member_order_roles_and_gaps() -> None:
    elements = json.loads(FIXTURE.read_text())["elements"]
    complete = relation_to_route(elements[0], datetime(2026, 10, 9, tzinfo=UTC))
    assert complete.geometry_quality == "complete"
    assert complete.geometry is not None
    assert complete.geometry.coordinates[0] == (5.0, 52.0)
    assert complete.geometry.coordinates[2] == (5.001, 52.0)
    assert complete.is_loop is True
    assert complete.source_type == "official_trail"
    assert complete.elevation_gain_m is None
    assert complete.raw_metadata["geometry_normalizer"] == "osm-v2"

    broken = relation_to_route(elements[1])
    assert broken.geometry_quality == "incomplete"
    assert broken.geometry is not None and broken.geometry.type == "MultiLineString"
    assert len(broken.geometry.coordinates) == 2
    assert broken.distance_m is None
    assert broken.is_loop is None

    absent = relation_to_route(elements[2])
    assert absent.geometry is None
    assert absent.distance_m is None
    assert absent.geometry_quality == "incomplete"
    empty_members = relation_to_route(
        {"type": "relation", "id": 104, "tags": {"route": "hiking"}, "members": []}
    )
    assert empty_members.geometry_quality == "incomplete"


def test_reconstructs_unmarked_reversed_way_without_bridging() -> None:
    lines, quality = reconstruct_members(
        [
            {"type": "way", "role": "", "geometry": [{"lon": 1, "lat": 1}, {"lon": 2, "lat": 2}]},
            {"type": "way", "role": "", "geometry": [{"lon": 3, "lat": 3}, {"lon": 2, "lat": 2}]},
        ]
    )
    assert lines == [[(1, 1), (2, 2), (3, 3)]]
    assert quality == "complete"
    nested, nested_quality = reconstruct_members(
        [{"type": "relation", "ref": 8, "role": "", "members": []}]
    )
    assert nested == []
    assert nested_quality == "incomplete"


@pytest.mark.parametrize(
    "invalid_point",
    [
        {"lon": 5.001},
        {"lon": 181, "lat": 52},
        {"lon": 5.001, "lat": float("nan")},
        {"lon": 5.001, "lat": "bad"},
        {"lon": True, "lat": 52},
        None,
    ],
)
def test_invalid_middle_member_coordinate_is_never_dropped_and_bridged(
    invalid_point: object,
) -> None:
    element = {
        "type": "relation",
        "id": 444,
        "tags": {"route": "hiking"},
        "members": [
            {
                "type": "way",
                "role": "",
                "geometry": [{"lon": 5.0, "lat": 52.0}, {"lon": 5.001, "lat": 52.0}],
            },
            {
                "type": "way",
                "role": "",
                "geometry": [
                    {"lon": 5.001, "lat": 52.0},
                    invalid_point,
                    {"lon": 5.003, "lat": 52.0},
                ],
            },
            {
                "type": "way",
                "role": "",
                "geometry": [{"lon": 5.003, "lat": 52.0}, {"lon": 5.004, "lat": 52.0}],
            },
        ],
    }
    route = relation_to_route(element)
    assert route.geometry_quality == "incomplete"
    assert route.geometry is not None and route.geometry.type == "MultiLineString"
    assert route.geometry.coordinates == [
        [(5.0, 52.0), (5.001, 52.0)],
        [(5.003, 52.0), (5.004, 52.0)],
    ]
    assert route.distance_m is None
    assert route.is_loop is None
    content = route_gpx(route).decode()
    assert content.count("<trkseg>") == 2
    assert content.count("<trkpt ") == 4


def test_relation_records_only_conservative_member_grade_separation_clues() -> None:
    element = {
        "type": "relation",
        "id": 202,
        "tags": {"route": "hiking", "name": "Clue route"},
        "members": [
            {
                "type": "way",
                "role": "",
                "tags": {"bridge": "yes", "layer": "1"},
                "geometry": [{"lon": 5, "lat": 52}, {"lon": 5.001, "lat": 52}],
            },
            {
                "type": "way",
                "role": "",
                "tags": {"layer": "1"},
                "geometry": [{"lon": 5.001, "lat": 52}, {"lon": 5.002, "lat": 52}],
            },
        ],
    }
    route = relation_to_route(element)
    assert route.raw_metadata["bridge_tunnel"] == "mixed_or_present"
    assert route.raw_metadata["layer"] == "1"

    element["members"][1]["tags"]["layer"] = "0"
    mixed = relation_to_route(element)
    assert "layer" not in mixed.raw_metadata


@pytest.mark.asyncio
async def test_osm_search_uses_bounded_post_query_and_fixture(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=json.loads(FIXTURE.read_text()))

    settings = load_settings()
    transport = httpx.MockTransport(handler)
    async with HTTPClient(settings, transport=transport) as client:
        routes = await OSMProvider(settings, client).search(
            (51.9, 4.9, 52.1, 5.1), activity=Activity.hiking
        )
    assert len(routes) == 2
    assert routes[0].provider == "osm"
    assert seen[0].method == "POST"
    query = parse_qs(seen[0].content.decode())["data"][0]
    assert "51.900000,4.900000,52.100000,5.100000" in query
    assert "out body geom 200" in query
    assert '["route"~"^(hiking|foot)$"]' in query


@pytest.mark.asyncio
async def test_get_fetches_one_numeric_relation_by_id() -> None:
    fixture = json.loads(FIXTURE.read_text())["elements"][0]
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"elements": [fixture]})

    settings = load_settings()
    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        route = await OSMProvider(settings, client).get("101")
    assert route.provider_route_id == "101"
    assert "relation(101)" in parse_qs(observed[0].content.decode())["data"][0]
    with pytest.raises(OutdoorError):
        await OSMProvider(settings, client).get("../../101")


@pytest.mark.asyncio
async def test_get_rejects_mismatched_relation_id_and_oversized_ids() -> None:
    fixture = json.loads(FIXTURE.read_text())["elements"][0]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"elements": [fixture]})

    settings = load_settings()
    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OutdoorError) as mismatch:
            await OSMProvider(settings, client).get("102")
        assert mismatch.value.code == "ROUTE_NOT_FOUND"
        with pytest.raises(OutdoorError) as too_long:
            await OSMProvider(settings, client).get("9" * 20)
    assert too_long.value.code == "INVALID_ROUTE_ID"


@pytest.mark.asyncio
async def test_overpass_partial_remark_is_reported_or_typed_when_empty() -> None:
    fixture = json.loads(FIXTURE.read_text())["elements"][0]

    async def with_elements(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"elements": [fixture], "remark": "partial result"})

    settings = load_settings()
    settings.discovery_cache_ttl_seconds = 0
    async with HTTPClient(settings, transport=httpx.MockTransport(with_elements)) as client:
        provider = OSMProvider(settings, client)
        routes = await provider.search((51.9, 4.9, 52.1, 5.1), activity=Activity.hiking)
        assert routes
        assert "Overpass returned a partial response." in provider.warnings

    async def without_elements(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"elements": [], "remark": "runtime error"})

    async with HTTPClient(settings, transport=httpx.MockTransport(without_elements)) as client:
        with pytest.raises(OutdoorError) as error:
            await OSMProvider(settings, client).search(
                (51.9, 4.9, 52.1, 5.1), activity=Activity.hiking
            )
    assert error.value.code == "PROVIDER_PARTIAL_RESPONSE"


@pytest.mark.asyncio
async def test_relation_output_limit_adds_truncation_warning() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.loads(FIXTURE.read_text()))

    settings = load_settings()
    settings.discovery_cache_ttl_seconds = 0
    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        provider = OSMProvider(settings, client)
        await provider.search((51.9, 4.9, 52.1, 5.1), activity=Activity.hiking, limit=1)
    assert any("additional matches may be omitted" in warning for warning in provider.warnings)


def test_query_rejects_unbounded_or_invalid_boxes() -> None:
    from outdoor_agent.providers.osm import overpass_query

    with pytest.raises(OutdoorError):
        overpass_query((-91, -181, 91, 181))


@pytest.mark.asyncio
async def test_osm_implements_typed_detail_and_export_protocols() -> None:
    settings = load_settings()
    async with HTTPClient(
        settings, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as http:
        provider: OSMProvider = OSMProvider(settings, http)
        detail: RouteDetailProvider = provider
        exporter: RouteExportProvider = provider
        assert detail.declaration.provider == "osm"
        assert exporter.declaration.provider == "osm"
