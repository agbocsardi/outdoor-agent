from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.closures import ClosureEvent
from outdoor_agent.models.routes import Activity, Geometry, Route
from outdoor_agent.providers.http import HTTPClient
from outdoor_agent.providers.ndw import FeedFetchResult, FeedSnapshot
from outdoor_agent.providers.osm_restrictions import (
    OSM_ATTRIBUTION,
    OSMRestrictionsProvider,
    overpass_restriction_query,
)
from outdoor_agent.services.validation import assess_route


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", **kwargs)


def _route() -> Route:
    return Route(
        id="trail-route",
        provider="local",
        provider_route_id="trail-route",
        attribution="local",
        name="trail",
        activity=Activity.hiking,
        geometry=Geometry(type="LineString", coordinates=[(5.1, 52.1), (5.1, 52.101)]),
        geometry_quality="complete",
    )


@pytest.mark.asyncio
async def test_overpass_returns_attributed_timed_unknown_access_events(tmp_path: Path) -> None:
    document = {
        "elements": [
            {
                "type": "way",
                "id": 77,
                "tags": {"highway": "path", "foot": "no", "name": "Forest path"},
                "geometry": [{"lat": 52.1, "lon": 5.1}, {"lat": 52.101, "lon": 5.1}],
            },
            {
                "type": "way",
                "id": 80,
                "tags": {"access": "no", "foot": "yes", "bicycle": "yes"},
                "geometry": [{"lat": 52.1, "lon": 5.1}, {"lat": 52.101, "lon": 5.1}],
            },
            {
                "type": "way",
                "id": 81,
                "tags": {"foot": "no", "bridge": "yes", "layer": "1"},
                "geometry": [{"lat": 52.1, "lon": 5.1}, {"lat": 52.101, "lon": 5.1}],
            },
            {
                "type": "way",
                "id": 82,
                "tags": {"construction": "no", "name": "not construction"},
                "geometry": [{"lat": 52.1, "lon": 5.1}, {"lat": 52.101, "lon": 5.1}],
            },
            {
                "type": "way",
                "id": 78,
                "tags": {"highway": "construction", "construction": "path"},
                "geometry": [{"lat": 52.1, "lon": 5.1}, {"lat": 52.101, "lon": 5.1}],
            },
            {
                "type": "way",
                "id": 79,
                "tags": {"bicycle:conditional": "no @ (Mo-Fr 08:00-17:00)"},
                "geometry": [{"lat": 52.1, "lon": 5.1}, {"lat": 52.101, "lon": 5.1}],
            },
        ]
    }
    requested_query: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_query.append(parse_qs(request.content.decode())["data"][0])
        return httpx.Response(
            200,
            json=document,
            headers={"ETag": '"osm-v1"', "Content-Type": "application/json"},
        )

    settings = _settings(tmp_path)
    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = await OSMRestrictionsProvider(settings, client).list_events(
            (5.0999, 52.1, 5.1001, 52.101), "2026-10-10", "hiking"
        )

    assert not result.errors
    assert len(result.snapshots) == 1
    events = {event.id: event for event in result.snapshots[0].events}
    assert set(events) == {"osm-way-77", "osm-way-78", "osm-way-79", "osm-way-80", "osm-way-81"}
    access = events["osm-way-77"]
    assert access.affected_modes == ["foot"]
    assert access.timing_quality == "unknown"
    assert access.source_url == "https://www.openstreetmap.org/way/77"
    assert OSM_ATTRIBUTION in access.restriction_evidence
    assert events["osm-way-78"].mode_evidence == "scope"
    assert events["osm-way-79"].mode_evidence == "unknown"
    assert "conditional" in " ".join(events["osm-way-79"].restriction_evidence)
    assert events["osm-way-80"].affected_modes == ["road"]
    assert events["osm-way-81"].grade_separation_evidence == ["bridge=yes", "layer=1"]
    query = requested_query[0]
    assert "[out:json]" in query
    assert '"foot"~"^(no|private)$"' in query
    assert '"construction"' in query
    assert "out tags geom 1000;" in query


def test_overpass_query_expands_route_bbox_and_rejects_huge_bounds() -> None:
    query = overpass_restriction_query((5.1, 52.1, 5.101, 52.102), buffer_m=20)
    assert "52.099819" in query and "52.102181" in query
    with pytest.raises(OutdoorError, match="too large"):
        overpass_restriction_query((-5, 40, 20, 60))


@pytest.mark.asyncio
async def test_offline_cache_miss_and_disabled_policy_are_reported(tmp_path: Path) -> None:
    offline = await OSMRestrictionsProvider(_settings(tmp_path, offline=True)).list_events(
        (5.0999, 52.1, 5.1001, 52.101), "2026-10-10", "hiking"
    )
    assert not offline.snapshots
    assert offline.errors[0].code == "OFFLINE_CACHE_MISS"

    disabled = await OSMRestrictionsProvider(
        _settings(tmp_path, disabled_providers=["osm"])
    ).list_events((5.0999, 52.1, 5.1001, 52.101), "2026-10-10", "hiking")
    assert disabled.errors[0].code == "PROVIDER_DISABLED"


@pytest.mark.asyncio
async def test_opt_in_finds_trail_closure_missing_from_ndw(tmp_path: Path, monkeypatch) -> None:
    import outdoor_agent.services.validation as validation

    now = datetime.now(UTC)
    ndw = FeedSnapshot(
        events=[],
        fetched_at=now,
        source_timestamp=now,
        stale=False,
        from_cache=False,
        source="https://ndw.example/current",
        warnings=[],
    )
    event = ClosureEvent(
        id="osm-way-77",
        provider="osm",
        source_url="https://www.openstreetmap.org/way/77",
        description="access restriction: path",
        geometry=Geometry(type="LineString", coordinates=[(5.1, 52.1), (5.1, 52.101)]),
        geometry_quality="precise",
        affected_modes=["foot"],
        mode_evidence="explicit",
        restriction_type="access restriction",
        status="active",
        timing_quality="unknown",
        observed_at=now,
        restriction_evidence=["foot=no", OSM_ATTRIBUTION],
    )
    snapshot = FeedSnapshot(
        events=[event],
        fetched_at=now,
        source_timestamp=None,
        stale=False,
        from_cache=False,
        source="https://www.openstreetmap.org/",
        warnings=["Optional OSM only."],
    )

    async def list_events(self, bbox, window, mode):
        return FeedFetchResult(snapshots=[snapshot], errors=[])

    monkeypatch.setattr(validation.OSMRestrictionsProvider, "list_events", list_events)
    injected = FeedFetchResult(snapshots=[ndw], errors=[])
    report = await assess_route(
        _settings(tmp_path),
        _route(),
        now.date().isoformat(),
        Activity.hiking,
        feed_result=injected,
        osm_restrictions=True,
    )
    assert any(item.id == event.id for item in report.events)
    assert any(
        item.event_id == event.id and item.classification == "possible_conflict"
        for item in report.findings
    )
    assert report.unknown_coverage is True
    assert any(
        "unmapped and untimed trail closures remain unknown" in warning
        for warning in report.warnings
    )
    assert injected.snapshots == [ndw]
    assert injected.errors == []


@pytest.mark.asyncio
async def test_optional_osm_runs_when_ndw_is_disabled_and_keeps_ndw_error(
    tmp_path: Path, monkeypatch
) -> None:
    import outdoor_agent.services.validation as validation
    from outdoor_agent.errors import OutdoorError

    async def list_events(self, bbox, window, mode):
        return FeedFetchResult(
            snapshots=[
                FeedSnapshot(
                    events=[],
                    fetched_at=datetime.now(UTC),
                    source_timestamp=None,
                    stale=False,
                    from_cache=False,
                    source="https://www.openstreetmap.org/",
                    warnings=[],
                )
            ],
            errors=[],
        )

    monkeypatch.setattr(validation.OSMRestrictionsProvider, "list_events", list_events)
    settings = _settings(tmp_path, disabled_providers=["ndw"])
    with pytest.raises(OutdoorError, match="NDW is disabled"):
        await assess_route(settings, _route(), "2026-10-10", Activity.hiking)

    report = await assess_route(
        settings,
        _route(),
        "2026-10-10",
        Activity.hiking,
        osm_restrictions=True,
    )
    assert report.source_errors[0].source == "ndw"
    assert report.source_errors[0].code == "PROVIDER_DISABLED"
    assert report.source_status == "partial"
    assert report.unknown_coverage is True
