"""Offline checks for private, idempotent GPX artifact storage."""

import os
import sqlite3
import stat

import pytest

from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import inspect_gpx
from outdoor_agent.models.routes import SourceType
from outdoor_agent.storage.db import RouteStore
from outdoor_agent.storage.migrations import SCHEMA_VERSION

GPX = (
    b'<gpx version="1.1" creator="synthetic storage test" '
    b'xmlns="http://www.topografix.com/GPX/1/1">'
    b"<trk><name>synthetic path</name><trkseg>"
    b'<trkpt lat="52" lon="4"/><trkpt lat="52.001" lon="4.001"/>'
    b"</trkseg></trk></gpx>"
)


def test_import_is_idempotent_preserves_original_bytes_and_first_provenance(tmp_path) -> None:
    first = inspect_gpx(
        GPX,
        source_url="https://ridewithgps.com/routes/123",
        source_type=SourceType.community,
        attribution="Original synthetic author",
    )
    second = inspect_gpx(
        GPX,
        source_url="https://wikiloc.com/routes/123",
        source_type=SourceType.generated,
        attribution="Later competing attribution",
    )
    store = RouteStore(tmp_path / "private-data")

    imported = store.import_route(first, GPX)
    repeated = store.import_route(second, GPX)

    assert imported.already_present is False
    assert repeated.already_present is True
    assert repeated.inspection.sha256 == first.sha256
    assert repeated.inspection.route.source_url == "https://ridewithgps.com/routes/123"
    assert repeated.inspection.route.source_type == SourceType.community
    assert repeated.inspection.route.attribution == "Original synthetic author"
    artifact = tmp_path / "private-data" / "gpx" / f"{first.sha256}.gpx"
    assert artifact.read_bytes() == GPX
    assert repeated.artifact.sha256 == first.sha256
    assert repeated.artifact.byte_count == len(GPX)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_created_storage_directories_database_and_artifact_are_private(tmp_path) -> None:
    inspection = inspect_gpx(GPX)
    data_dir = tmp_path / "private-data"

    result = RouteStore(data_dir).import_route(inspection, GPX)

    modes = {
        "data directory": stat.S_IMODE(data_dir.stat().st_mode),
        "artifact directory": stat.S_IMODE((data_dir / "gpx").stat().st_mode),
        "database": stat.S_IMODE((data_dir / "state.sqlite3").stat().st_mode),
        "GPX artifact": stat.S_IMODE(os.stat(result.artifact.path).st_mode),
    }
    assert modes == {
        "data directory": 0o700,
        "artifact directory": 0o700,
        "database": 0o600,
        "GPX artifact": 0o600,
    }


def test_hash_mismatch_is_rejected_before_storage_mutation(tmp_path) -> None:
    inspection = inspect_gpx(GPX)
    data_dir = tmp_path / "private-data"

    with pytest.raises(OutdoorError) as raised:
        RouteStore(data_dir).import_route(inspection, GPX + b" ")

    assert raised.value.code == "ARTIFACT_HASH_MISMATCH"
    assert not data_dir.exists()


def test_missing_or_changed_cached_artifact_is_reported_as_corrupt(tmp_path) -> None:
    inspection = inspect_gpx(GPX)
    store = RouteStore(tmp_path / "private-data")
    imported = store.import_route(inspection, GPX)
    artifact_path = tmp_path / "private-data" / "gpx" / f"{inspection.sha256}.gpx"

    artifact_path.write_bytes(GPX + b" changed")
    with pytest.raises(OutdoorError) as altered:
        store.import_route(inspection, GPX)
    assert altered.value.code == "CACHE_CORRUPT"

    artifact_path.write_bytes(GPX)
    artifact_path.unlink()
    with pytest.raises(OutdoorError) as missing:
        store.import_route(inspection, GPX)
    assert missing.value.code == "CACHE_CORRUPT"
    assert imported.already_present is False


def test_corrupt_sqlite_file_is_reported_without_exposing_database_contents(tmp_path) -> None:
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()
    (data_dir / "state.sqlite3").write_bytes(b"synthetic-not-a-database-secret")

    with pytest.raises(OutdoorError) as raised:
        RouteStore(data_dir).import_route(inspect_gpx(GPX), GPX)

    assert raised.value.code == "STORAGE_FAILED"
    assert "synthetic-not-a-database-secret" not in str(raised.value)


def test_newer_schema_version_is_reported_as_migration_error(tmp_path) -> None:
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()
    database = data_dir / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(OutdoorError) as raised:
        RouteStore(data_dir).import_route(inspect_gpx(GPX), GPX)

    assert raised.value.code == "CACHE_VERSION_UNSUPPORTED"


def test_invalid_cached_inspection_is_a_typed_corruption_error(tmp_path) -> None:
    import sqlite3

    content = b'<gpx version="1.1"><wpt lat="52" lon="5"/></gpx>'
    inspection = inspect_gpx(content)
    store = RouteStore(tmp_path / "state")
    store.import_route(inspection, content)
    with sqlite3.connect(tmp_path / "state/state.sqlite3") as db:
        db.execute("UPDATE routes SET inspection_json = '{}' ")
    with pytest.raises(OutdoorError) as error:
        store.import_route(inspection, content)
    assert error.value.code == "CACHE_CORRUPT"
