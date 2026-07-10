"""提供 Cursor 消费者所需的安全队列事务。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from scheduler.db import QueueDB
from scheduler.schema import canonical_evidence


JOB_STATUSES = ("pending", "processing", "retry_wait", "done", "cancelled")


class ConsumerQueue:
    """补充只属于在线消费者的 owner 约束操作与安全查询。"""

    def __init__(self, database: QueueDB) -> None:
        """复用 QueueDB 的数据库路径和连接设置。"""
        self.db = database

    @staticmethod
    def _owned(job: Optional[sqlite3.Row], owner: str, now: int) -> bool:
        return bool(
            job is not None
            and job["status"] == "processing"
            and job["lease_owner"] == owner
            and job["lease_until"] is not None
            and job["lease_until"] > now
        )

    @staticmethod
    def _job(
        connection: sqlite3.Connection, job_id: int
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()

    def renew(
        self, job_id: int, owner: str, now: int, lease_seconds: int
    ) -> bool:
        """原子续租任务及同 owner 的未决 final 预约。"""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        lease_until = now + lease_seconds * 1000
        with self.db._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._job(connection, job_id)
            final = connection.execute(
                "SELECT * FROM deliveries WHERE job_id=? AND kind='final'",
                (job_id,),
            ).fetchone()
            if not self._owned(job, owner, now):
                connection.rollback()
                return False
            if final is not None and not self._renewable(final, owner, now):
                connection.rollback()
                return False
            connection.execute(
                "UPDATE jobs SET lease_until=?,updated_at=? WHERE id=?",
                (lease_until, now, job_id),
            )
            if final is not None:
                connection.execute(
                    "UPDATE deliveries SET lease_until=? "
                    "WHERE job_id=? AND kind='final'",
                    (lease_until, job_id),
                )
            connection.commit()
        return True

    @staticmethod
    def _renewable(
        delivery: sqlite3.Row, owner: str, now: int
    ) -> bool:
        if delivery["state"] == "failed":
            return True
        return (
            delivery["state"] == "claimed"
            and delivery["owner"] == owner
            and delivery["lease_until"] > now
        )

    def append_evidence_owned(
        self, job_id: int, evidence: Dict[str, Any], owner: str, now: int
    ) -> bool:
        """仅由有效任务 owner 原子追加严格 JSON 证据。"""
        encoded, evidence_id = canonical_evidence(evidence)
        with self.db._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._job(connection, job_id)
            if not self._owned(job, owner, now):
                connection.rollback()
                return False
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO evidence_keys(
                    job_id,evidence_id,created_at
                ) VALUES (?,?,?)
                """,
                (job_id, evidence_id, now),
            ).rowcount
            if inserted == 0:
                connection.commit()
                return True
            items = json.loads(job["evidence_json"])
            if not isinstance(items, list):
                raise ValueError("invalid evidence storage")
            stored = json.loads(encoded)
            stored["evidence_id"] = evidence_id
            items.append(stored)
            connection.execute(
                "UPDATE jobs SET evidence_json=?,updated_at=? WHERE id=?",
                (
                    json.dumps(
                        items, ensure_ascii=False, separators=(",", ":"),
                        allow_nan=False,
                    ),
                    now,
                    job_id,
                ),
            )
            connection.commit()
        return True

    def prepare_final(
        self,
        job_id: int,
        owner: str,
        reply_record: Dict[str, Any],
        now: int,
        lease_seconds: int,
    ) -> bool:
        """Atomically persist reply material and claim final delivery."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        encoded = json.dumps(
            reply_record, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        )
        with self.db._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._job(connection, job_id)
            delivery = connection.execute(
                "SELECT state FROM deliveries WHERE job_id=? AND kind='final'",
                (job_id,),
            ).fetchone()
            if not self._owned(job, owner, now):
                connection.rollback()
                return False
            if delivery is not None and delivery["state"] != "failed":
                connection.rollback()
                return False
            self._write_final_claim(
                connection, job_id, owner, now, lease_seconds, delivery
            )
            connection.execute(
                """
                INSERT INTO final_records(
                    job_id,reply_json,record_state,created_at,updated_at
                ) VALUES (?,?,'prepared',?,?)
                ON CONFLICT(job_id) DO UPDATE SET reply_json=excluded.reply_json,
                    record_state='prepared',record_error=NULL,
                    updated_at=excluded.updated_at
                """,
                (job_id, encoded, now, now),
            )
            connection.commit()
        return True

    @staticmethod
    def _write_final_claim(
        connection: sqlite3.Connection,
        job_id: int,
        owner: str,
        now: int,
        lease_seconds: int,
        delivery: Optional[sqlite3.Row],
    ) -> None:
        if delivery is None:
            connection.execute(
                """
                INSERT INTO deliveries(
                    job_id,kind,state,owner,lease_until,attempted_at
                ) VALUES (?,'final','claimed',?,?,?)
                """,
                (job_id, owner, now + lease_seconds * 1000, now),
            )
            return
        connection.execute(
            """
            UPDATE deliveries SET state='claimed',owner=?,lease_until=?,
                attempted_at=?,sent_at=NULL,last_error=NULL
            WHERE job_id=? AND kind='final' AND state='failed'
            """,
            (owner, now + lease_seconds * 1000, now, job_id),
        )

    def complete_final_pending(
        self, job_id: int, owner: str, now: int
    ) -> bool:
        """Atomically mark final sent, job done, and local record pending."""
        with self.db._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._job(connection, job_id)
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE job_id=? AND kind='final'",
                (job_id,),
            ).fetchone()
            if not self._owned(job, owner, now):
                connection.rollback()
                return False
            if not self._live_delivery(delivery, owner, now):
                connection.rollback()
                return False
            changed = self._write_final_done(connection, job_id, owner, now)
            if not changed:
                connection.rollback()
                return False
            connection.commit()
        return True

    @staticmethod
    def _live_delivery(
        delivery: Optional[sqlite3.Row], owner: str, now: int
    ) -> bool:
        return bool(
            delivery is not None
            and delivery["state"] == "claimed"
            and delivery["owner"] == owner
            and delivery["lease_until"] > now
        )

    @staticmethod
    def _write_final_done(
        connection: sqlite3.Connection, job_id: int, owner: str, now: int
    ) -> bool:
        delivery = connection.execute(
            """
            UPDATE deliveries SET state='sent',sent_at=?,owner=NULL,
                lease_until=NULL,last_error=NULL
            WHERE job_id=? AND kind='final' AND state='claimed' AND owner=?
              AND lease_until > ?
            """,
            (now, job_id, owner, now),
        )
        job = connection.execute(
            """
            UPDATE jobs SET status='done',final_sent_at=?,lease_owner=NULL,
                lease_until=NULL,updated_at=?
            WHERE id=? AND status='processing' AND lease_owner=?
              AND lease_until > ?
            """,
            (now, now, job_id, owner, now),
        )
        record = connection.execute(
            """
            UPDATE final_records SET record_state='pending',updated_at=?
            WHERE job_id=? AND record_state='prepared'
            """,
            (now, job_id),
        )
        return delivery.rowcount == job.rowcount == record.rowcount == 1

    def pending_record(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Return persisted reply material only when local recording is pending."""
        with self.db._connect() as connection:
            row = connection.execute(
                """
                SELECT reply_json FROM final_records
                WHERE job_id=? AND record_state='pending'
                """,
                (job_id,),
            ).fetchone()
        return None if row is None else json.loads(row["reply_json"])

    def mark_recorded(self, job_id: int, now: int) -> bool:
        """Clear record_pending after an idempotent local record succeeds."""
        with self.db._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE final_records SET record_state='recorded',
                    record_error=NULL,updated_at=?
                WHERE job_id=? AND record_state='pending'
                  AND EXISTS(
                    SELECT 1 FROM jobs WHERE id=? AND status='done'
                  )
                """,
                (now, job_id, job_id),
            )
            connection.commit()
        return cursor.rowcount == 1

    def reconcile(
        self,
        job_id: int,
        action: str,
        now: int,
        confirm_duplicate_risk: bool = False,
    ) -> Optional[str]:
        """Resolve an uncertain final delivery as an explicit operator action."""
        if action not in ("mark-done", "cancel", "retry"):
            raise ValueError("invalid reconciliation action")
        if action == "retry" and not confirm_duplicate_risk:
            return None
        with self.db._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                "SELECT state FROM deliveries WHERE job_id=? AND kind='final'",
                (job_id,),
            ).fetchone()
            if delivery is None or delivery["state"] != "uncertain":
                connection.rollback()
                return None
            status = self._apply_reconciliation(
                connection, job_id, action, now
            )
            connection.execute(
                """
                INSERT INTO manual_actions(
                    job_id,action,risk_confirmed,acted_at
                ) VALUES (?,?,?,?)
                """,
                (job_id, action, int(confirm_duplicate_risk), now),
            )
            connection.commit()
        return status

    @staticmethod
    def _apply_reconciliation(
        connection: sqlite3.Connection, job_id: int, action: str, now: int
    ) -> str:
        if action == "mark-done":
            connection.execute(
                "UPDATE deliveries SET state='sent',sent_at=?,last_error=NULL "
                "WHERE job_id=? AND kind='final'", (now, job_id)
            )
            connection.execute(
                "UPDATE jobs SET status='done',final_sent_at=?,"
                "lease_owner=NULL,lease_until=NULL,updated_at=? "
                "WHERE id=?", (now, now, job_id)
            )
            connection.execute(
                "UPDATE final_records SET record_state='pending',updated_at=? "
                "WHERE job_id=? AND record_state='prepared'", (now, job_id)
            )
            return "done"
        status = "cancelled" if action == "cancel" else "retry_wait"
        delivery_state = "uncertain" if action == "cancel" else "failed"
        connection.execute(
            "UPDATE deliveries SET state=?,last_error=? "
            "WHERE job_id=? AND kind='final'",
            (delivery_state, "Manual" + action.title(), job_id),
        )
        connection.execute(
            "UPDATE jobs SET status=?,available_at=?,lease_owner=NULL,"
            "lease_until=NULL,updated_at=? WHERE id=?",
            (status, now, now, job_id),
        )
        return status

    def cancel(self, job_id: int, owner: str, now: int) -> bool:
        """仅允许有效任务 owner 在没有未决发送时取消。"""
        with self.db._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._job(connection, job_id)
            claimed = connection.execute(
                "SELECT 1 FROM deliveries WHERE job_id=? AND ("
                "state='claimed' OR ("
                "kind='final' AND state IN ('uncertain','sent')))",
                (job_id,),
            ).fetchone()
            if not self._owned(job, owner, now) or claimed is not None:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE jobs SET status='cancelled',lease_owner=NULL,
                    lease_until=NULL,updated_at=? WHERE id=?
                """,
                (now, job_id),
            )
            connection.commit()
        return True

    def list_jobs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回不包含 payload、证据和错误正文的任务摘要。"""
        if status is not None and status not in JOB_STATUSES:
            raise ValueError("invalid status")
        sql = (
            "SELECT jobs.id,conv_id,mid,status,attempts,network_mode,"
            "jobs.lease_until,available_at,d.state AS final_delivery_state,"
            "d.last_error AS block_reason,"
            "CASE WHEN f.record_state='pending' THEN 1 ELSE 0 END "
            "AS record_pending FROM jobs "
            "LEFT JOIN deliveries d ON d.job_id=jobs.id AND d.kind='final' "
            "LEFT JOIN final_records f ON f.job_id=jobs.id"
        )
        parameters = ()
        if status is not None:
            sql += " WHERE jobs.status=?"
            parameters = (status,)
        sql += " ORDER BY detected_at,jobs.id"
        with self.db._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        results = [dict(row) for row in rows]
        for result in results:
            result["record_pending"] = bool(result["record_pending"])
        return results
