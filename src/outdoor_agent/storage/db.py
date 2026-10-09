"""Private, idempotent local route storage with unchanged GPX artifacts."""

import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import GPXArtifact, GPXInspection, ImportResult, now
from outdoor_agent.storage.migrations import migrate


class RouteStore:
    """Create state only for imports, never inspection/provider diagnostics."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def import_route(self, inspection: GPXInspection, content: bytes) -> ImportResult:
        if hashlib.sha256(content).hexdigest() != inspection.sha256:
            raise OutdoorError("ARTIFACT_HASH_MISMATCH", "GPX bytes do not match inspection.", 7)
        try:
            self.data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            artifacts = self.data_dir / "gpx"
            artifacts.mkdir(mode=0o700, exist_ok=True)
            db_path = self.data_dir / "state.sqlite3"
            # Create with private permissions before SQLite opens it.
            fd = os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(db_path, timeout=10)) as connection:
                migrate(connection)
                # Serialize repeat/concurrent imports; first provenance wins.
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT inspection_json, artifact_path FROM routes WHERE id = ?",
                    (inspection.route.id,),
                ).fetchone()
                if existing:
                    try:
                        stored = GPXInspection.model_validate_json(existing[0])
                    except ValidationError as exc:
                        raise OutdoorError(
                            "CACHE_CORRUPT",
                            "Stored inspection is malformed. Restore local state from backup.",
                            7,
                        ) from exc
                    artifact_path = Path(existing[1])
                    saved_content = b""
                    if artifact_path.is_file():
                        with artifact_path.open("rb") as file:
                            saved_content = file.read(len(content) + 1)
                    if (
                        not artifact_path.is_file()
                        or hashlib.sha256(saved_content).hexdigest() != inspection.sha256
                    ):
                        raise OutdoorError(
                            "CACHE_CORRUPT",
                            "Stored GPX is missing or altered. Restore from your original file/backup.",
                            7,
                        )
                    return self._result(stored, artifact_path, True)
                artifact_path = artifacts / f"{inspection.sha256}.gpx"
                temporary: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(dir=artifacts, delete=False) as file:
                        temporary = file.name
                        file.write(content)
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(temporary, artifact_path)
                    temporary = None
                finally:
                    if temporary is not None:
                        Path(temporary).unlink(missing_ok=True)
                connection.execute(
                    "INSERT INTO routes VALUES (?, ?, ?, ?, ?)",
                    (
                        inspection.route.id,
                        inspection.sha256,
                        inspection.model_dump_json(),
                        str(artifact_path),
                        now().isoformat(),
                    ),
                )
                connection.commit()
                return self._result(inspection, artifact_path, False)
        except (OSError, sqlite3.Error) as exc:
            raise OutdoorError(
                "STORAGE_FAILED", "Could not save route to local SQLite/artifact storage.", 7
            ) from exc

    @staticmethod
    def _result(inspection: GPXInspection, path: Path, present: bool) -> ImportResult:
        return ImportResult(
            inspection=inspection,
            artifact=GPXArtifact(
                route_id=inspection.route.id,
                path=str(path),
                sha256=inspection.sha256,
                byte_count=inspection.byte_count,
            ),
            already_present=present,
        )
