"""Versioned, lossless SQLite migrations."""

import sqlite3

from outdoor_agent.errors import OutdoorError

SCHEMA_VERSION = 2


def migrate(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise OutdoorError(
                "CACHE_VERSION_UNSUPPORTED",
                "Local state was created by a newer outdoor-agent version.",
                7,
            )
        if version == 0:
            connection.execute("""CREATE TABLE routes (
                id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
                inspection_json TEXT NOT NULL, artifact_path TEXT NOT NULL,
                imported_at TEXT NOT NULL)""")
        if version < 2:
            connection.execute("""CREATE TABLE route_cache (
                id TEXT PRIMARY KEY, route_json TEXT NOT NULL,
                artifact_path TEXT, sha256 TEXT, stored_at TEXT NOT NULL, artifact_original INTEGER NOT NULL DEFAULT 1)""")
            connection.execute("""CREATE TABLE closure_checks (
                route_id TEXT NOT NULL, request_key TEXT NOT NULL, finding_hash TEXT NOT NULL,
                report_json TEXT NOT NULL, checked_at TEXT NOT NULL,
                PRIMARY KEY(route_id, request_key))""")
            connection.execute("""CREATE TABLE delivery_audit (
                idempotency_key TEXT PRIMARY KEY, route_id TEXT NOT NULL,
                target TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        connection.execute("PRAGMA user_version = 2")
