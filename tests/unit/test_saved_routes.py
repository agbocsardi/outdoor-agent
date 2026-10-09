import sqlite3
from pathlib import Path

import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import inspect_gpx
from outdoor_agent.models.routes import Geometry, Route, SourceType
from outdoor_agent.services.delivery import export_route, prepare_handoff
from outdoor_agent.storage.db import RouteStore


def config(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")


@pytest.mark.asyncio
async def test_local_export_and_default_dry_run_are_lossless(
    tmp_path: Path, example_gpx: Path
) -> None:
    settings = config(tmp_path)
    store = RouteStore(settings.data_dir)
    inspection = inspect_gpx(example_gpx.read_bytes())
    store.import_route(inspection, example_gpx.read_bytes())
    before = (settings.data_dir / "state.sqlite3").read_bytes()
    planned = await prepare_handoff(settings, inspection.route.id)
    assert planned.dry_run is True
    assert planned.status == "manual_handoff_planned"
    assert not Path(planned.path).exists()
    assert (settings.data_dir / "state.sqlite3").read_bytes() == before
    output = tmp_path / "export.gpx"
    exported = await export_route(settings, inspection.route.id, output)
    assert output.read_bytes() == example_gpx.read_bytes()
    assert exported.original_bytes is True
    assert exported.status == "exported"


@pytest.mark.asyncio
async def test_confirmed_handoff_idempotent_and_never_claims_sync(
    tmp_path: Path, example_gpx: Path
) -> None:
    settings = config(tmp_path)
    store = RouteStore(settings.data_dir)
    inspection = inspect_gpx(example_gpx.read_bytes())
    store.import_route(inspection, example_gpx.read_bytes())
    first = await prepare_handoff(
        settings, inspection.route.id, confirm=True, account="My COROS account"
    )
    stamp = Path(first.path).stat().st_mtime_ns
    second = await prepare_handoff(
        settings, inspection.route.id, confirm=True, account="My COROS account"
    )
    assert first.status == "manual_handoff_prepared"
    assert first.account == "My COROS account"
    assert second.already_prepared is True
    assert Path(second.path).stat().st_mtime_ns == stamp
    assert first.watch_status == "watch_sync_unverified"
    assert first.instructions
    assert "submitted" not in first.status
    with sqlite3.connect(settings.data_dir / "state.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM delivery_audit").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_mapped_geometry_export_preserves_segments_and_origin(tmp_path: Path) -> None:
    settings = config(tmp_path)
    route = Route(
        id="osm:42",
        provider="osm",
        provider_route_id="42",
        source_url="https://www.openstreetmap.org/relation/42",
        source_type=SourceType.official_trail,
        attribution="Synthetic OSM attribution",
        name="Synthetic mapped trail",
        geometry=Geometry(
            type="MultiLineString", coordinates=[[(5, 52), (5.001, 52)], [(6, 53), (6.001, 53)]]
        ),
        gpx_access="available",
    )
    RouteStore(settings.data_dir).save_route(route)
    output = tmp_path / "mapped.gpx"
    first = await export_route(settings, route.id, output)
    second = await export_route(settings, route.id, output)
    result = inspect_gpx(output.read_bytes())
    assert result.track_segment_count == 2
    assert result.route.source_url == route.source_url
    assert route.attribution in result.route.description
    assert first.original_bytes is False
    assert second.original_bytes is False
    assert len(RouteStore(settings.data_dir).list_routes()) == 1


@pytest.mark.asyncio
async def test_export_refuses_unapproved_overwrite(tmp_path: Path, example_gpx: Path) -> None:
    settings = config(tmp_path)
    inspection = inspect_gpx(example_gpx.read_bytes())
    RouteStore(settings.data_dir).import_route(inspection, example_gpx.read_bytes())
    output = tmp_path / "existing.gpx"
    output.write_bytes(b"private existing file")
    with pytest.raises(OutdoorError) as error:
        await export_route(settings, inspection.route.id, output)
    assert error.value.code == "OUTPUT_EXISTS"
    assert output.read_bytes() == b"private existing file"
    await export_route(settings, inspection.route.id, output, overwrite=True)
    assert output.read_bytes() == example_gpx.read_bytes()


def test_schema_one_migration_retains_original_import(tmp_path: Path, example_gpx: Path) -> None:
    settings = config(tmp_path)
    inspection = inspect_gpx(example_gpx.read_bytes())
    settings.data_dir.mkdir()
    with sqlite3.connect(settings.data_dir / "state.sqlite3") as db:
        db.execute(
            "CREATE TABLE routes (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, inspection_json TEXT NOT NULL, artifact_path TEXT NOT NULL, imported_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO routes VALUES (?, ?, ?, ?, ?)",
            (
                inspection.route.id,
                inspection.sha256,
                inspection.model_dump_json(),
                "not-read-in-this-test",
                inspection.route.fetched_at.isoformat(),
            ),
        )
        db.execute("PRAGMA user_version=1")
    store = RouteStore(settings.data_dir)
    assert store.get_route(inspection.route.id).source_url == inspection.route.source_url
    store.save_route(
        inspection.route.model_copy(
            update={"id": "osm:42", "provider": "osm", "provider_route_id": "42"}
        )
    )
    assert len(store.list_routes()) == 2
    with sqlite3.connect(settings.data_dir / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            db.execute("SELECT inspection_json FROM routes").fetchone()[0]
            == inspection.model_dump_json()
        )


def test_stable_recheck_comparison_and_no_state_listing(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = RouteStore(settings.data_dir)
    assert store.list_routes() == []
    assert not settings.data_dir.exists()
    first = store.record_check("local:fixture", "date|bike", {"status": "known_conflict"}, ["A"])
    second = store.record_check("local:fixture", "date|bike", {"status": "known_conflict"}, ["A"])
    changed = store.record_check(
        "local:fixture", "date|bike", {"status": "possible_conflict"}, ["B"]
    )
    assert first["changed"] is None
    assert second["changed"] is False
    assert changed["changed"] is True


@pytest.mark.asyncio
async def test_unknown_target_and_confirmation_flags(tmp_path: Path) -> None:
    settings = config(tmp_path)
    with pytest.raises(OutdoorError) as error:
        await prepare_handoff(settings, "local:fixture", target="unverified")
    assert error.value.exit_code == 5
    with pytest.raises(OutdoorError) as error:
        await prepare_handoff(settings, "local:fixture", confirm=True, dry_run=True)
    assert error.value.code == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_refreshed_geometry_invalidates_old_export_and_handoff(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = RouteStore(settings.data_dir)
    route = Route(
        id="osm:relation:42",
        provider="osm",
        provider_route_id="42",
        attribution="Synthetic OSM attribution",
        name="Synthetic route",
        geometry=Geometry(type="LineString", coordinates=[(5, 52), (5.001, 52)]),
    )
    store.save_route(route)
    first = await prepare_handoff(settings, route.id, confirm=True)
    original = Path(first.path).read_bytes()
    route = route.model_copy(
        update={"geometry": Geometry(type="LineString", coordinates=[(5, 52), (5.002, 52)])}
    )
    store.save_route(route)
    assert store.artifact_bytes(route.id) is None
    second = await prepare_handoff(settings, route.id, confirm=True)
    assert second.idempotency_key != first.idempotency_key
    assert second.path != first.path
    assert Path(second.path).read_bytes() != original
    assert Path(first.path).read_bytes() == original
