"""提供通知发送的事务性预约与最多一次状态记录。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Union

from scheduler.db import QueueDB
from scheduler.errors import safe_error_category
from scheduler.schema import initialize_database


KINDS = ("ack", "partial", "final")
MARKERS = {"ack": "ack_sent_at", "partial": "partial_sent_at"}


class Outbox:
    """封装同一 SQLite 队列中的事务性通知发送预约。"""

    def __init__(
        self, database: Union[QueueDB, str, os.PathLike]
    ) -> None:
        """接收 QueueDB 或数据库路径并确保预约表存在。"""
        self.path = database.path if isinstance(database, QueueDB) else str(database)
        initialize_database(self.path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _validate(kind: str, lease_seconds: Optional[int] = None) -> None:
        if kind not in KINDS:
            raise ValueError("invalid delivery kind")
        if lease_seconds is not None and lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

    def claim(
        self,
        job_id: int,
        kind: str,
        owner: str,
        now: int,
        lease_seconds: int,
    ) -> bool:
        """为非终态任务创建独占发送预约，过期预约转为不确定。"""
        self._validate(kind, lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                """
                SELECT status,lease_owner,lease_until
                FROM jobs WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if kind == "ack" and connection.execute(
                "SELECT 1 FROM deliveries "
                "WHERE job_id = ? AND kind = 'partial'",
                (job_id,),
            ).fetchone() is not None:
                connection.commit()
                return False
            if self._settle_or_find_claim(connection, job_id, now):
                connection.commit()
                return False
            if not self._job_allows_claim(job, kind, owner, now):
                connection.commit()
                return False
            claimed = self._write_claim(
                connection, job_id, kind, owner, now,
                now + lease_seconds * 1000,
            )
            connection.commit()
        return claimed

    @staticmethod
    def _job_allows_claim(
        job: Optional[sqlite3.Row], kind: str, owner: str, now: int
    ) -> bool:
        if job is None or job["status"] in ("done", "cancelled"):
            return False
        if kind != "final":
            return True
        return (
            job["status"] == "processing"
            and job["lease_owner"] == owner
            and job["lease_until"] is not None
            and job["lease_until"] > now
        )

    @staticmethod
    def _settle_or_find_claim(
        connection: sqlite3.Connection, job_id: int, now: int
    ) -> bool:
        rows = connection.execute(
            "SELECT lease_until FROM deliveries "
            "WHERE job_id = ? AND state = 'claimed'",
            (job_id,),
        ).fetchall()
        if not rows:
            return False
        connection.execute(
            """
            UPDATE deliveries
            SET state = 'uncertain', owner = NULL, lease_until = NULL,
                last_error = 'LeaseExpired'
            WHERE job_id = ? AND state = 'claimed' AND lease_until <= ?
            """,
            (job_id, now),
        )
        return True

    @staticmethod
    def _expire_claim(
        connection: sqlite3.Connection,
        job_id: int,
        kind: str,
        now: int,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE deliveries
            SET state='uncertain', owner=NULL, lease_until=NULL,
                last_error='LeaseExpired'
            WHERE job_id=? AND kind=? AND state='claimed'
              AND lease_until <= ?
            """,
            (job_id, kind, now),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _write_claim(
        connection: sqlite3.Connection,
        job_id: int,
        kind: str,
        owner: str,
        now: int,
        lease_until: int,
    ) -> bool:
        row = connection.execute(
            "SELECT state FROM deliveries WHERE job_id = ? AND kind = ?",
            (job_id, kind),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO deliveries(
                    job_id,kind,state,owner,lease_until,attempted_at
                ) VALUES (?,?,'claimed',?,?,?)
                """,
                (job_id, kind, owner, lease_until, now),
            )
            return True
        if row["state"] != "failed":
            return False
        connection.execute(
            """
            UPDATE deliveries
            SET state='claimed', owner=?, lease_until=?, attempted_at=?,
                sent_at=NULL, last_error=NULL
            WHERE job_id=? AND kind=? AND state='failed'
            """,
            (owner, lease_until, now, job_id, kind),
        )
        return True

    def mark_sent(
        self, job_id: int, kind: str, owner: str, sent_at: int
    ) -> bool:
        """原子完成有效预约，并同步确认或阶段任务标记。"""
        self._validate(kind)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE job_id=? AND kind=?",
                (job_id, kind),
            ).fetchone()
            if not self._can_finish(
                connection, delivery, job_id, kind, owner, sent_at
            ):
                connection.rollback()
                return False
            if kind == "final":
                synchronized = self._complete_job(
                    connection, job_id, owner, sent_at
                )
            else:
                synchronized = self._sync_marker(
                    connection, job_id, kind, sent_at
                )
            if not synchronized:
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                UPDATE deliveries SET state='sent', sent_at=?,
                    owner=NULL, lease_until=NULL, last_error=NULL
                WHERE job_id=? AND kind=? AND state='claimed' AND owner=?
                """,
                (sent_at, job_id, kind, owner),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
        return True

    @staticmethod
    def _can_finish(
        connection: sqlite3.Connection,
        delivery: Optional[sqlite3.Row],
        job_id: int,
        kind: str,
        owner: str,
        now: int,
    ) -> bool:
        if (
            delivery is None
            or delivery["state"] != "claimed"
            or delivery["owner"] != owner
            or delivery["lease_until"] <= now
        ):
            return False
        job = connection.execute(
            "SELECT status,lease_owner,lease_until FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if not Outbox._job_allows_claim(job, kind, owner, now):
            return False
        return kind != "final" or job["lease_owner"] == delivery["owner"]

    @staticmethod
    def _complete_job(
        connection: sqlite3.Connection,
        job_id: int,
        owner: str,
        sent_at: int,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET status='done', final_sent_at=?, lease_owner=NULL,
                lease_until=NULL, updated_at=?
            WHERE id=? AND status='processing' AND lease_owner=?
              AND lease_until > ?
            """,
            (sent_at, sent_at, job_id, owner, sent_at),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _sync_marker(
        connection: sqlite3.Connection, job_id: int, kind: str, sent_at: int
    ) -> bool:
        marker = MARKERS.get(kind)
        if marker is None:
            return True
        cursor = connection.execute(
            """
            UPDATE jobs SET {0}=?, updated_at=?
            WHERE id=? AND {0} IS NULL
              AND status IN ('pending','processing','retry_wait')
            """.format(marker),
            (sent_at, sent_at, job_id),
        )
        return cursor.rowcount == 1

    def mark_failed(
        self,
        job_id: int,
        kind: str,
        owner: str,
        now: int,
        error: str,
        uncertain: bool = False,
    ) -> bool:
        """把对应预约记为明确失败或结果不确定，并清除租约。"""
        self._validate(kind)
        if kind == "final":
            raise ValueError("use fail_final for final delivery")
        state = "uncertain" if uncertain else "failed"
        safe_error = self._safe_error(error)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._expire_claim(connection, job_id, kind, now):
                connection.commit()
                return False
            cursor = connection.execute(
                """
                UPDATE deliveries
                SET state=?, owner=NULL, lease_until=NULL,
                    last_error=?, attempted_at=?
                WHERE job_id=? AND kind=? AND state='claimed' AND owner=?
                  AND lease_until > ?
                """,
                (state, safe_error, now, job_id, kind, owner, now),
            )
            connection.commit()
        return cursor.rowcount == 1

    def renew(
        self,
        job_id: int,
        kind: str,
        owner: str,
        now: int,
        lease_seconds: int,
    ) -> bool:
        """续期有效预约；正式回复还要求同 owner 的任务租约有效。"""
        self._validate(kind, lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE job_id=? AND kind=?",
                (job_id, kind),
            ).fetchone()
            if not self._can_finish(
                connection, delivery, job_id, kind, owner, now
            ):
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                UPDATE deliveries SET lease_until=?
                WHERE job_id=? AND kind=? AND state='claimed' AND owner=?
                """,
                (now + lease_seconds * 1000, job_id, kind, owner),
            )
            connection.commit()
        return cursor.rowcount == 1

    def mark_final_uncertain(
        self, job_id: int, owner: str, now: int, error: str
    ) -> bool:
        """Freeze a claimed final when the external send result is unknown."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._expire_claim(connection, job_id, "final", now):
                connection.commit()
                return True
            cursor = connection.execute(
                """
                UPDATE deliveries SET state='uncertain',owner=NULL,
                    lease_until=NULL,last_error=?,attempted_at=?
                WHERE job_id=? AND kind='final' AND state='claimed'
                  AND owner=?
                """,
                (self._safe_error(error), now, job_id, owner),
            )
            connection.commit()
        return cursor.rowcount == 1

    def fail_final(
        self,
        job_id: int,
        owner: str,
        now: int,
        error: str,
        available_at: int,
        uncertain: bool = False,
    ) -> bool:
        """原子记录正式发送失败，并释放任务或冻结不确定结果。"""
        state = "uncertain" if uncertain else "failed"
        safe_error = self._safe_error(error)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._expire_claim(
                connection, job_id, "final", now
            ):
                connection.commit()
                return False
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE job_id=? AND kind='final'",
                (job_id,),
            ).fetchone()
            if not self._can_finish(
                connection, delivery, job_id, "final", owner, now
            ):
                connection.rollback()
                return False
            job_changed = self._release_final_job(
                connection, job_id, owner, now, available_at, safe_error
            )
            delivery_changed = self._settle_final_failure(
                connection, job_id, owner, now, state, safe_error
            )
            if not job_changed or not delivery_changed:
                connection.rollback()
                return False
            connection.commit()
        return True

    @staticmethod
    def _release_final_job(
        connection: sqlite3.Connection,
        job_id: int, owner: str, now: int,
        available_at: int, error: str,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE jobs SET status='retry_wait', last_error=?,
                available_at=?, lease_owner=NULL, lease_until=NULL, updated_at=?
            WHERE id=? AND status='processing' AND lease_owner=?
              AND lease_until > ?
            """,
            (error, available_at, now, job_id, owner, now),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _settle_final_failure(
        connection: sqlite3.Connection,
        job_id: int, owner: str, now: int,
        state: str, error: str,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE deliveries SET state=?, owner=NULL, lease_until=NULL,
                last_error=?, attempted_at=?
            WHERE job_id=? AND kind='final' AND state='claimed'
              AND owner=? AND lease_until > ?
            """,
            (state, error, now, job_id, owner, now),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _safe_error(error: str) -> str:
        return safe_error_category(error)

    def state(self, job_id: int, kind: str) -> Optional[Dict[str, Any]]:
        """返回指定发送预约的持久状态，不存在时返回 None。"""
        self._validate(kind)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE job_id=? AND kind=?",
                (job_id, kind),
            ).fetchone()
        return None if row is None else dict(row)
