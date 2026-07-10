"""Centralized, versioned SQLite schema and migrations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, Union


CURRENT_SCHEMA_VERSION = 2
STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS jobs (
     id INTEGER PRIMARY KEY AUTOINCREMENT,
     conv_id TEXT NOT NULL,
     mid INTEGER NOT NULL,
     payload_json TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'pending'
      CHECK(status IN ('pending','processing','retry_wait','done','cancelled')),
     network_mode TEXT NOT NULL DEFAULT 'unknown'
      CHECK(network_mode IN ('unknown','none','fast_http','browser')),
     detected_at INTEGER NOT NULL,
     available_at INTEGER NOT NULL,
     lease_owner TEXT,
     lease_until INTEGER,
     attempts INTEGER NOT NULL DEFAULT 0,
     last_error TEXT,
     ack_sent_at INTEGER,
     partial_sent_at INTEGER,
     final_sent_at INTEGER,
     evidence_json TEXT NOT NULL DEFAULT '[]',
     created_at INTEGER NOT NULL,
     updated_at INTEGER NOT NULL,
     UNIQUE(conv_id, mid)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_available
    ON jobs(status, available_at, detected_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS deliveries(
     job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
     kind TEXT NOT NULL CHECK(kind IN('ack','partial','final')),
     state TEXT NOT NULL CHECK(state IN('claimed','sent','failed','uncertain')),
     owner TEXT,
     lease_until INTEGER,
     attempted_at INTEGER NOT NULL,
     sent_at INTEGER,
     last_error TEXT,
     PRIMARY KEY(job_id,kind)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deliveries_state_lease
    ON deliveries(state,lease_until)
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_keys(
     job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
     evidence_id TEXT NOT NULL,
     created_at INTEGER NOT NULL,
     PRIMARY KEY(job_id,evidence_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS final_records(
     job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
     reply_json TEXT NOT NULL,
     record_state TEXT NOT NULL
      CHECK(record_state IN('prepared','pending','recorded')),
     record_error TEXT,
     created_at INTEGER NOT NULL,
     updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS manual_actions(
     id INTEGER PRIMARY KEY AUTOINCREMENT,
     job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
     action TEXT NOT NULL,
     risk_confirmed INTEGER NOT NULL DEFAULT 0,
     acted_at INTEGER NOT NULL
    )
    """,
)


def canonical_evidence(evidence: Dict[str, Any]) -> tuple:
    """Return canonical evidence JSON and a stable SHA-256 identifier."""
    value = dict(evidence)
    value.pop("evidence_id", None)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _seed_evidence_keys(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT id,evidence_json,created_at FROM jobs"
    ).fetchall()
    for job_id, encoded, created_at in rows:
        try:
            items = json.loads(encoded)
        except (TypeError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            _, evidence_id = canonical_evidence(item)
            connection.execute(
                """
                INSERT OR IGNORE INTO evidence_keys(
                    job_id,evidence_id,created_at
                ) VALUES (?,?,?)
                """,
                (job_id, evidence_id, created_at),
            )


def _migrate_zero_to_one(connection: sqlite3.Connection) -> None:
    for statement in STATEMENTS:
        connection.execute(statement)
    _seed_evidence_keys(connection)


def _migrate_one_to_two(connection: sqlite3.Connection) -> None:
    """Record the centralized migration runner introduced after version one."""
    for statement in STATEMENTS:
        connection.execute(statement)


def _migrate(connection: sqlite3.Connection, version: int) -> None:
    while version < CURRENT_SCHEMA_VERSION:
        if version == 0:
            _migrate_zero_to_one(connection)
        elif version == 1:
            _migrate_one_to_two(connection)
        else:
            raise RuntimeError("unsupported schema version")
        version += 1
        connection.execute("PRAGMA user_version={}".format(version))


def initialize_database(path: Union[str, os.PathLike]) -> None:
    """Run only required migrations under one immediate writer lock."""
    connection = sqlite3.connect(str(path), timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError("database schema is newer than this application")
        if version == CURRENT_SCHEMA_VERSION:
            return
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError("database schema is newer than this application")
        _migrate(connection, version)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
