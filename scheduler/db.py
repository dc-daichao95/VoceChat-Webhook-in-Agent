"""Durable SQLite queue with per-conversation FIFO leasing."""

import json
import sqlite3


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
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
);
CREATE INDEX IF NOT EXISTS idx_jobs_available
ON jobs(status, available_at, detected_at);
"""

UNFINISHED = ("pending", "processing", "retry_wait")


class QueueDB:
    """Persist jobs and enforce idempotent queue state transitions."""

    def __init__(self, path):
        """Initialize the queue database and schema at *path*."""
        self.path = str(path)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["evidence"] = json.loads(result.pop("evidence_json"))
        return result

    def enqueue(self, payload, detected_at):
        """Idempotently enqueue a payload and return its stable job ID."""
        conv_id = payload["conv_id"]
        mid = payload["mid"]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO jobs (
                    conv_id, mid, payload_json, detected_at, available_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conv_id, mid) DO NOTHING
                """,
                (conv_id, mid, encoded, detected_at, detected_at,
                 detected_at, detected_at),
            )
            row = connection.execute(
                "SELECT id FROM jobs WHERE conv_id = ? AND mid = ?",
                (conv_id, mid),
            ).fetchone()
            connection.commit()
        return row["id"]

    def get(self, job_id):
        """Return one decoded job, or ``None`` when it does not exist."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._decode(row)

    def find(self, conv_id, mid):
        """Find and decode a job by its idempotency key."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE conv_id = ? AND mid = ?",
                (conv_id, mid),
            ).fetchone()
        return self._decode(row)

    def claim(self, owner, now, limit, lease_seconds):
        """Lease FIFO-ready jobs from at most *limit* conversations."""
        if limit <= 0:
            return []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT current.id
                FROM jobs AS current
                WHERE current.status IN ('pending', 'retry_wait')
                  AND current.available_at <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM jobs AS earlier
                    WHERE earlier.conv_id = current.conv_id
                      AND earlier.status IN ('pending','processing','retry_wait')
                      AND (
                        earlier.detected_at < current.detected_at
                        OR (earlier.detected_at = current.detected_at
                            AND earlier.id < current.id)
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM jobs AS active
                    WHERE active.conv_id = current.conv_id
                      AND active.status = 'processing'
                  )
                ORDER BY current.detected_at, current.id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            job_ids = [row["id"] for row in rows]
            self._claim_ids(
                connection, job_ids, owner, now, lease_seconds
            )
            jobs = self._select_ids(connection, job_ids)
            connection.commit()
        return [self._decode(row) for row in jobs]

    @staticmethod
    def _claim_ids(connection, job_ids, owner, now, lease_seconds):
        for job_id in job_ids:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'processing', lease_owner = ?, lease_until = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (owner, now + lease_seconds, now, job_id),
            )

    @staticmethod
    def _select_ids(connection, job_ids):
        if not job_ids:
            return []
        placeholders = ",".join("?" for _ in job_ids)
        rows = connection.execute(
            "SELECT * FROM jobs WHERE id IN ({})".format(placeholders),
            job_ids,
        ).fetchall()
        positions = {job_id: index for index, job_id in enumerate(job_ids)}
        return sorted(rows, key=lambda row: positions[row["id"]])

    def renew(self, job_id, owner, now, lease_seconds):
        """Extend a processing lease held by *owner*."""
        return self._transition(
            """
            UPDATE jobs SET lease_until = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_owner = ?
            """,
            (now + lease_seconds, now, job_id, owner),
        )

    def recover_expired(self, now):
        """Move expired processing leases into retry-wait state."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'retry_wait', available_at = ?,
                    lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE status = 'processing' AND lease_until < ?
                """,
                (now, now, now),
            )
            connection.commit()
        return cursor.rowcount

    def mark_ack_sent(self, job_id, sent_at):
        """Set the acknowledgement timestamp exactly once."""
        return self._mark_sent("ack_sent_at", job_id, sent_at)

    def mark_partial_sent(self, job_id, sent_at):
        """Set the partial-response timestamp exactly once."""
        return self._mark_sent("partial_sent_at", job_id, sent_at)

    def _mark_sent(self, column, job_id, sent_at):
        sql = """
            UPDATE jobs SET {0} = ?, updated_at = ?
            WHERE id = ? AND {0} IS NULL
              AND status IN ('pending','processing','retry_wait')
        """.format(column)
        return self._transition(sql, (sent_at, sent_at, job_id))

    def append_evidence(self, job_id, evidence, now):
        """Append one JSON evidence object to a job."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT evidence_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(job_id)
            items = json.loads(row["evidence_json"])
            items.append(evidence)
            connection.execute(
                "UPDATE jobs SET evidence_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(items, ensure_ascii=False, separators=(",", ":")),
                 now, job_id),
            )
            connection.commit()

    def complete(self, job_id, owner, sent_at):
        """Complete a processing job held by *owner*."""
        return self._transition(
            """
            UPDATE jobs
            SET status = 'done', final_sent_at = ?, lease_owner = NULL,
                lease_until = NULL, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_owner = ?
            """,
            (sent_at, sent_at, job_id, owner),
        )

    def fail(self, job_id, owner, error, available_at):
        """Schedule a processing job for retry when owned by *owner*."""
        return self._transition(
            """
            UPDATE jobs
            SET status = 'retry_wait', last_error = ?, available_at = ?,
                lease_owner = NULL, lease_until = NULL, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_owner = ?
            """,
            (error, available_at, available_at, job_id, owner),
        )

    def cancel(self, job_id, now):
        """Cancel an unfinished job exactly once."""
        return self._transition(
            """
            UPDATE jobs
            SET status = 'cancelled', lease_owner = NULL, lease_until = NULL,
                updated_at = ?
            WHERE id = ? AND status IN ('pending','processing','retry_wait')
            """,
            (now, job_id),
        )

    def due_for_ack(self, now, delay_ms):
        """Return unfinished jobs whose acknowledgement deadline has passed."""
        return self._due("ack_sent_at", now, delay_ms)

    def due_for_partial(self, now, delay_ms):
        """Return unfinished jobs whose partial-response deadline has passed."""
        return self._due("partial_sent_at", now, delay_ms)

    def _due(self, column, now, delay_ms):
        sql = """
            SELECT * FROM jobs
            WHERE status IN ('pending','processing','retry_wait')
              AND {0} IS NULL AND detected_at + ? <= ?
            ORDER BY detected_at, id
        """.format(column)
        with self._connect() as connection:
            rows = connection.execute(sql, (delay_ms, now)).fetchall()
        return [self._decode(row) for row in rows]

    def _transition(self, sql, parameters):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(sql, parameters)
            connection.commit()
        return cursor.rowcount == 1
