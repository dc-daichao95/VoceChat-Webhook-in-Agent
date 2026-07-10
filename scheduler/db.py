"""提供带租约和幂等状态转换的 SQLite 持久任务队列。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from scheduler.schema import initialize_database

MAX_PROCESSING_CONVERSATIONS = 3
NETWORK_MODES = ("unknown", "none", "fast_http", "browser")


class QueueDataError(ValueError):
    """标识某个持久任务字段包含损坏或错误类型的 JSON。"""

    def __init__(self, job_id: int, field: str, cause: BaseException) -> None:
        """保存任务、字段及原始解码错误。"""
        self.job_id = job_id
        self.field = field
        self.cause = cause
        super().__init__(
            "job {} has corrupt {}: {}".format(job_id, field, cause)
        )


class QueueDB:
    """封装原子队列；owner 必须是每个消费者进程或会话的唯一 ID。"""

    def __init__(self, path: Union[str, os.PathLike]) -> None:
        """初始化数据库文件及任务表。"""
        self.path = str(path)
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
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        job_id = result["id"]
        result["payload"] = QueueDB._load_json(
            job_id, "payload_json", result.pop("payload_json"), dict
        )
        result["evidence"] = QueueDB._load_json(
            job_id, "evidence_json", result.pop("evidence_json"), list
        )
        return result

    @staticmethod
    def _load_json(
        job_id: int, field: str, encoded: str, expected_type: type
    ) -> Any:
        try:
            value = json.loads(encoded)
        except (TypeError, ValueError) as cause:
            raise QueueDataError(job_id, field, cause) from cause
        if not isinstance(value, expected_type):
            cause = TypeError(
                "expected {}, got {}".format(
                    expected_type.__name__, type(value).__name__
                )
            )
            raise QueueDataError(job_id, field, cause)
        return value

    @staticmethod
    def _lease_until(now: int, lease_seconds: int) -> int:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return now + lease_seconds * 1000

    def enqueue(self, payload: Dict[str, Any], detected_at: int) -> int:
        """幂等入队，并返回稳定的任务 ID。"""
        job_id, _ = self.enqueue_with_created(payload, detected_at)
        return job_id

    def enqueue_with_created(
        self, payload: Dict[str, Any], detected_at: int
    ) -> Tuple[int, bool]:
        """幂等入队，返回稳定任务 ID 及本次是否首次创建。"""
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        key = (payload["conv_id"], payload["mid"])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    conv_id, mid, payload_json, detected_at, available_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conv_id, mid) DO NOTHING
                """,
                key + (encoded, detected_at, detected_at, detected_at, detected_at),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT id FROM jobs WHERE conv_id = ? AND mid = ?", key
            ).fetchone()
            connection.commit()
        return int(row["id"]), created

    def get(self, job_id: int) -> Dict[str, Any]:
        """按 ID 返回已解码任务；任务不存在时抛出 KeyError。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._decode(row)

    def find(self, conv_id: str, mid: int) -> Optional[Dict[str, Any]]:
        """按幂等键查找任务，不存在时返回 None。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE conv_id = ? AND mid = ?",
                (conv_id, mid),
            ).fetchone()
        return None if row is None else self._decode(row)

    def claim(
        self, owner: str, now: int, limit: int, lease_seconds: int
    ) -> List[Dict[str, Any]]:
        """按会话 FIFO 领取最多 limit 个不同会话的就绪任务。"""
        lease_until = self._lease_until(now, lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            capacity = self._claim_capacity(connection, limit)
            claimed = self._claim_valid_jobs(
                connection, owner, now, lease_until, capacity
            )
            connection.commit()
        return claimed

    def _claim_valid_jobs(
        self,
        connection: sqlite3.Connection,
        owner: str,
        now: int,
        lease_until: int,
        capacity: int,
    ) -> List[Dict[str, Any]]:
        claimed = []
        while len(claimed) < capacity:
            row = self._next_claim_candidate(connection, now)
            if row is None:
                break
            try:
                self._decode(row)
            except QueueDataError as error:
                self._cancel_corrupt(connection, error, now)
                continue
            self._claim_id(connection, row["id"], owner, now, lease_until)
            claimed.append(self._decode(self._row_by_id(connection, row["id"])))
        return claimed

    @staticmethod
    def _next_claim_candidate(
        connection: sqlite3.Connection, now: int
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            """
            SELECT candidate.*
            FROM jobs AS candidate
            WHERE candidate.status IN ('pending', 'retry_wait')
              AND candidate.available_at <= ?
              AND NOT EXISTS (
                SELECT 1 FROM jobs AS earlier
                WHERE earlier.conv_id = candidate.conv_id
                  AND earlier.status IN ('pending','processing','retry_wait')
                  AND earlier.mid < candidate.mid
              )
              AND NOT EXISTS (
                SELECT 1 FROM jobs AS active
                WHERE active.conv_id = candidate.conv_id
                  AND active.status = 'processing'
              )
              AND NOT EXISTS (
                SELECT 1
                FROM jobs AS recorded_job
                JOIN final_records AS final_record
                  ON final_record.job_id = recorded_job.id
                WHERE recorded_job.conv_id = candidate.conv_id
                  AND recorded_job.mid < candidate.mid
                  AND final_record.record_state = 'pending'
              )
              AND NOT EXISTS (
                SELECT 1 FROM deliveries AS final_delivery
                WHERE final_delivery.job_id = candidate.id
                  AND final_delivery.kind = 'final'
                  AND final_delivery.state IN ('claimed','sent','uncertain')
              )
            ORDER BY candidate.detected_at, candidate.id
            LIMIT 1
            """,
            (now,),
        ).fetchone()

    @staticmethod
    def _claim_capacity(
        connection: sqlite3.Connection, limit: int
    ) -> int:
        active_count = connection.execute(
            """
            SELECT COUNT(DISTINCT conv_id)
            FROM jobs
            WHERE status = 'processing'
            """
        ).fetchone()[0]
        remaining = max(0, MAX_PROCESSING_CONVERSATIONS - active_count)
        return min(max(limit, 0), remaining)

    @staticmethod
    def _claim_id(
        connection: sqlite3.Connection,
        job_id: int,
        owner: str,
        now: int,
        lease_until: int,
    ) -> None:
        connection.execute(
            """
            UPDATE jobs
            SET status = 'processing', lease_owner = ?, lease_until = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE id = ?
            """,
            (owner, lease_until, now, job_id),
        )

    @staticmethod
    def _row_by_id(
        connection: sqlite3.Connection, job_id: int
    ) -> sqlite3.Row:
        return connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()

    @staticmethod
    def _cancel_corrupt(
        connection: sqlite3.Connection, error: QueueDataError, now: int
    ) -> None:
        connection.execute(
            """
            UPDATE jobs
            SET status = 'cancelled', lease_owner = NULL, lease_until = NULL,
                last_error = ?, updated_at = ?
            WHERE id = ? AND status IN ('pending','processing','retry_wait')
            """,
            (
                "corrupt {}: {}".format(error.field, error.cause),
                now,
                error.job_id,
            ),
        )

    def renew(
        self, job_id: int, owner: str, now: int, lease_seconds: int
    ) -> bool:
        """为指定 owner 持有的 processing 任务续租。"""
        lease_until = self._lease_until(now, lease_seconds)
        return self._transition(
            """
            UPDATE jobs SET lease_until = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_owner = ?
              AND lease_until > ?
            """,
            (lease_until, now, job_id, owner, now),
        )

    def recover_expired(self, now: int) -> int:
        """原子冻结已预约 final，再恢复过期 processing 任务。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE deliveries
                SET state='uncertain',owner=NULL,lease_until=NULL,
                    last_error='LeaseExpired'
                WHERE kind='final' AND state='claimed'
                  AND job_id IN (
                    SELECT id FROM jobs
                    WHERE status='processing' AND lease_until <= ?
                  )
                """,
                (now,),
            )
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'retry_wait', available_at = ?,
                    lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE status = 'processing' AND lease_until <= ?
                """,
                (now, now, now),
            )
            connection.commit()
        return cursor.rowcount

    def mark_ack_sent(self, job_id: int, sent_at: int) -> bool:
        """仅首次记录确认消息发送时间。"""
        return self._mark_sent("ack_sent_at", job_id, sent_at)

    def mark_partial_sent(self, job_id: int, sent_at: int) -> bool:
        """仅首次记录阶段消息发送时间。"""
        return self._mark_sent("partial_sent_at", job_id, sent_at)

    def _mark_sent(self, column: str, job_id: int, sent_at: int) -> bool:
        return self._transition(
            """
            UPDATE jobs SET {0} = ?, updated_at = ?
            WHERE id = ? AND {0} IS NULL
              AND status IN ('pending','processing','retry_wait')
            """.format(column),
            (sent_at, sent_at, job_id),
        )

    def _append_evidence_unchecked(
        self, job_id: int, evidence: Dict[str, Any], now: int
    ) -> None:
        """仅供迁移和测试装载不受租约约束的旧证据。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT evidence_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            items = self._load_json(
                job_id, "evidence_json", row["evidence_json"], list
            )
            items.append(evidence)
            connection.execute(
                "UPDATE jobs SET evidence_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(
                        items,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    now,
                    job_id,
                ),
            )
            connection.commit()

    def complete(self, job_id: int, owner: str, sent_at: int) -> bool:
        """拒绝绕过 final outbox；正式完成必须经 Outbox.mark_sent。"""
        return False

    def fail(
        self,
        job_id: int,
        owner: str,
        error: str,
        available_at: int,
        now: int,
    ) -> bool:
        """仅由租约 owner 将 processing 任务安排为稍后重试。"""
        return self._transition(
            """
            UPDATE jobs
            SET status = 'retry_wait', last_error = ?, available_at = ?,
                lease_owner = NULL, lease_until = NULL, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_owner = ?
              AND lease_until > ?
            """,
            (error, available_at, now, job_id, owner, now),
        )

    def set_network_mode(
        self, job_id: int, owner: str, mode: str, now: int
    ) -> bool:
        """为持有有效租约的任务记录受约束的网络执行模式。"""
        if mode not in NETWORK_MODES:
            raise ValueError("invalid network mode: {}".format(mode))
        return self._transition(
            """
            UPDATE jobs SET network_mode = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_owner = ?
              AND lease_until > ?
            """,
            (mode, now, job_id, owner, now),
        )

    def cancel(self, job_id: int, now: int) -> bool:
        """幂等取消尚未结束的任务。"""
        return self._transition(
            """
            UPDATE jobs
            SET status = 'cancelled', lease_owner = NULL, lease_until = NULL,
                updated_at = ?
            WHERE id = ? AND status IN ('pending','processing','retry_wait')
              AND NOT EXISTS (
                SELECT 1 FROM deliveries
                WHERE job_id = jobs.id
                  AND (
                    state='claimed'
                    OR (
                      kind='final' AND state IN ('uncertain','sent')
                    )
                  )
              )
            """,
            (now, job_id),
        )

    def due_for_ack(self, now: int, delay_ms: int) -> List[Dict[str, Any]]:
        """返回已到确认消息期限且尚未发送确认的未结束任务。"""
        return self._due(
            "ack_sent_at", now, delay_ms, "AND partial_sent_at IS NULL"
        )

    def due_for_partial(
        self, now: int, delay_ms: int
    ) -> List[Dict[str, Any]]:
        """返回已到阶段消息期限且尚未发送阶段消息的未结束任务。"""
        return self._due("partial_sent_at", now, delay_ms)

    def _due(
        self, column: str, now: int, delay_ms: int, extra: str = ""
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT * FROM jobs
            WHERE status IN ('pending','processing','retry_wait')
              AND {0} IS NULL AND detected_at + ? <= ? {1}
            ORDER BY detected_at, id
        """.format(column, extra)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(sql, (delay_ms, now)).fetchall()
            decoded = self._decode_isolating(connection, rows, now)
            connection.commit()
        return decoded

    def _decode_isolating(
        self,
        connection: sqlite3.Connection,
        rows: List[sqlite3.Row],
        now: int,
    ) -> List[Dict[str, Any]]:
        decoded = []
        for row in rows:
            try:
                decoded.append(self._decode(row))
            except QueueDataError as error:
                self._cancel_corrupt(connection, error, now)
        return decoded

    def _transition(self, sql: str, parameters: Tuple[Any, ...]) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(sql, parameters)
            connection.commit()
        return cursor.rowcount == 1
