"""Private, idempotent local route storage with unchanged GPX artifacts."""

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import ValidationError

from outdoor_agent.errors import OutdoorError
from outdoor_agent.models.routes import GPXArtifact, GPXInspection, ImportResult, Route, now
from outdoor_agent.providers.osm import OSM_GEOMETRY_NORMALIZER_VERSION
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

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Read-only queries never create state; writes migrate before their transaction."""
        path = self.data_dir / "state.sqlite3"
        if not write and not path.is_file():
            raise OutdoorError(
                "ROUTE_NOT_FOUND", "No local route state exists. Import a GPX or search first.", 2
            )
        try:
            if write:
                self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            uri = path.as_uri() + ("?mode=rw" if write else "?mode=ro")
            with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                if write:
                    migrate(db)
                with db:
                    yield db
        except (OSError, sqlite3.Error) as exc:
            raise OutdoorError(
                "STORAGE_FAILED", "Local SQLite operation failed; check state permissions.", 7
            ) from exc

    def save_route(
        self, route: Route, content: bytes | None = None, *, content_original: bool = True
    ) -> None:
        """Cache a provider route; original imports remain immutable."""
        path = None
        digest = None
        if content is not None:
            from outdoor_agent.geo.gpx import inspect_gpx

            inspection = inspect_gpx(content)
            digest = inspection.sha256
            from outdoor_agent.geo.export import write_gpx

            artifact_path = self.data_dir / "gpx" / f"{digest}.gpx"
            write_gpx(artifact_path, content)
            path = str(artifact_path)
        with self.connection(write=True) as db:
            db.execute(
                """INSERT INTO route_cache VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET route_json=excluded.route_json,
                artifact_path=excluded.artifact_path,
                sha256=excluded.sha256,
                stored_at=excluded.stored_at, artifact_original=excluded.artifact_original""",
                (
                    route.id,
                    route.model_dump_json(),
                    path,
                    digest,
                    now().isoformat(),
                    int(content_original),
                ),
            )

    def get_route(self, route_id: str) -> Route:
        with self.connection() as db:
            # Original local imports have precedence over cached provider representations.
            row = db.execute(
                "SELECT inspection_json FROM routes WHERE id=?", (route_id,)
            ).fetchone()
            try:
                if row:
                    return GPXInspection.model_validate_json(row[0]).route
                exists = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='route_cache'"
                ).fetchone()
                row = (
                    db.execute(
                        "SELECT route_json FROM route_cache WHERE id=?", (route_id,)
                    ).fetchone()
                    if exists
                    else None
                )
                if row:
                    return self._quarantine_legacy_osm(Route.model_validate_json(row[0]))
            except ValidationError as exc:
                raise OutdoorError(
                    "CACHE_CORRUPT", "Stored route metadata is malformed.", 7
                ) from exc
        raise OutdoorError(
            "ROUTE_NOT_FOUND", "Route is not saved locally. Run search, get or import-gpx first.", 2
        )

    def list_routes(self, limit: int = 100) -> list[Route]:
        if not (self.data_dir / "state.sqlite3").exists():
            return []
        with self.connection() as db:
            imports = db.execute(
                "SELECT inspection_json FROM routes ORDER BY imported_at DESC LIMIT ?", (limit,)
            ).fetchall()
            try:
                routes = [GPXInspection.model_validate_json(row[0]).route for row in imports]
                exists = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='route_cache'"
                ).fetchone()
                if exists:
                    ids = {route.id for route in routes}
                    for row in db.execute(
                        "SELECT route_json FROM route_cache ORDER BY stored_at DESC LIMIT ?",
                        (limit,),
                    ):
                        route = self._quarantine_legacy_osm(Route.model_validate_json(row[0]))
                        if route.id not in ids:
                            routes.append(route)
                            ids.add(route.id)
            except ValidationError as exc:
                raise OutdoorError(
                    "CACHE_CORRUPT", "Stored route metadata is malformed.", 7
                ) from exc
        return routes[:limit]

    @staticmethod
    def _quarantine_legacy_osm(route: Route) -> Route:
        """Hide pre-osm-v2 cached geometry until the provider safely refreshes it."""
        if (
            route.provider != "osm"
            or route.raw_metadata.get("geometry_normalizer") == OSM_GEOMETRY_NORMALIZER_VERSION
        ):
            return route
        warning = (
            "Legacy OSM geometry was quarantined because it predates safe normalization; "
            "refresh the route before using or exporting its geometry."
        )
        warnings = list(route.warnings)
        if warning not in warnings:
            warnings.append(warning)
        return route.model_copy(
            update={
                "geometry": None,
                "start_lat": None,
                "start_lon": None,
                "end_lat": None,
                "end_lon": None,
                "distance_m": None,
                "is_loop": None,
                "geometry_quality": "unknown",
                "gpx_access": "unverified",
                "warnings": warnings,
            }
        )

    def artifact_bytes(self, route_id: str) -> bytes | None:
        from outdoor_agent.geo.gpx import MAX_BYTES

        with self.connection() as db:
            row = db.execute(
                "SELECT artifact_path, sha256 FROM routes WHERE id=?", (route_id,)
            ).fetchone()
            if not row:
                exists = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='route_cache'"
                ).fetchone()
                row = (
                    db.execute(
                        "SELECT artifact_path, sha256, route_json FROM route_cache WHERE id=?",
                        (route_id,),
                    ).fetchone()
                    if exists
                    else None
                )
                if row:
                    try:
                        cached_route = Route.model_validate_json(row[2])
                    except ValidationError as exc:
                        raise OutdoorError(
                            "CACHE_CORRUPT", "Stored route metadata is malformed.", 7
                        ) from exc
                    if (
                        cached_route.provider == "osm"
                        and cached_route.raw_metadata.get("geometry_normalizer")
                        != OSM_GEOMETRY_NORMALIZER_VERSION
                    ):
                        raise OutdoorError(
                            "OSM_GEOMETRY_REFRESH_REQUIRED",
                            "Legacy cached OSM geometry and artifacts are quarantined. Run get --route-id <id> --refresh before export.",
                            5,
                            provider="osm",
                            suggestions=["Refresh the route with get --refresh, then export it."],
                        )
            if not row or row[0] is None:
                return None
            try:
                path = Path(row[0])
                path.resolve().relative_to((self.data_dir / "gpx").resolve())
                with path.open("rb") as file:
                    content = file.read(MAX_BYTES + 1)
            except (OSError, ValueError) as exc:
                raise OutdoorError(
                    "CACHE_CORRUPT", "Stored GPX artifact is missing or outside private state.", 7
                ) from exc
            if len(content) > MAX_BYTES or hashlib.sha256(content).hexdigest() != row[1]:
                raise OutdoorError(
                    "CACHE_CORRUPT", "Stored GPX artifact is oversized or altered.", 7
                )
            return content

    def record_check(
        self, route_id: str, request_key: str, report: dict[str, object], evidence: object
    ) -> dict[str, object]:
        """Compare stable findings, excluding collection times and transport metadata."""
        digest = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        with self.connection(write=True) as db:
            previous = db.execute(
                "SELECT finding_hash, checked_at FROM closure_checks WHERE route_id=? AND request_key=?",
                (route_id, request_key),
            ).fetchone()
            checked = now().isoformat()
            db.execute(
                """INSERT INTO closure_checks VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(route_id, request_key) DO UPDATE SET finding_hash=excluded.finding_hash,
                report_json=excluded.report_json, checked_at=excluded.checked_at""",
                (route_id, request_key, digest, json.dumps(report), checked),
            )
        return {
            "changed": None if previous is None else previous[0] != digest,
            "previous_checked_at": previous[1] if previous else None,
            "finding_hash": digest,
        }

    def delivery_record(self, key: str) -> dict[str, object] | None:
        if not (self.data_dir / "state.sqlite3").is_file():
            return None
        with self.connection() as db:
            if not db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_audit'"
            ).fetchone():
                return None
            row = db.execute(
                "SELECT result_json FROM delivery_audit WHERE idempotency_key=?", (key,)
            ).fetchone()
            return dict(json.loads(row[0])) if row else None

    def record_delivery(
        self, key: str, route_id: str, target: str, result: dict[str, object]
    ) -> None:
        with self.connection(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO delivery_audit VALUES (?, ?, ?, ?, ?)",
                (key, route_id, target, json.dumps(result), now().isoformat()),
            )

    def artifact_original(self, route_id: str) -> bool:
        with self.connection() as db:
            if db.execute("SELECT id FROM routes WHERE id=?", (route_id,)).fetchone():
                return True
            if not db.execute("SELECT name FROM sqlite_master WHERE name='route_cache'").fetchone():
                return False
            row = db.execute(
                "SELECT artifact_original FROM route_cache WHERE id=?", (route_id,)
            ).fetchone()
            return bool(row and row[0])
