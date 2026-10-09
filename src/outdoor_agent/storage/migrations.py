"""Versioned SQLite schema. Future versions must migrate without discarding data."""

import sqlite3

from outdoor_agent.errors import OutdoorError

SCHEMA_VERSION = 1


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
            connection.execute("""
                CREATE TABLE routes (
                    id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    inspection_json TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                )
            """)
            connection.execute("PRAGMA user_version = 1")
