"""Stage-aware final sending with durable local-record repair."""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.notifier import target_from_conv
from scheduler.outbox import Outbox
from scheduler.policy import retry_delay_seconds


Sender = Callable[..., Any]
Recorder = Callable[[Dict[str, Any]], None]
Clock = Callable[[], int]
FINAL_LEASE_SECONDS = 120
# These client rejections are defined before application processing starts.
# 408/409/429 are intentionally excluded because delivery may be ambiguous
# without a provider idempotency key.
DEFINITE_REJECTION_STATUSES = frozenset(
    (400, 401, 403, 404, 405, 410, 413, 414, 415, 422)
)


def _default_sender() -> Sender:
    from send import send_message

    return send_message


def _default_recorder() -> Recorder:
    from brain.recording import record_reply

    return record_reply


def _result(job_id: int, status: str, pending: bool = False) -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "record_pending": pending,
    }


def _reply_record(
    job: Dict[str, Any], reply: str, markdown: bool, now: int
) -> Dict[str, Any]:
    return {
        "conv_id": job["conv_id"],
        "mid": job["mid"],
        "reply": reply,
        "markdown": bool(markdown),
        "bot_uid": int(os.environ.get("BOT_UID", "0") or 0),
        "created_at": now,
    }


def _send(
    sender: Sender,
    server: str,
    api_key: str,
    record: Dict[str, Any],
) -> Any:
    target = target_from_conv(record["conv_id"])
    return sender(
        server,
        api_key,
        record["reply"],
        markdown=record["markdown"],
        timeout=30,
        **target
    )


def _fail_after_send_attempt(
    db: QueueDB,
    job: Dict[str, Any],
    owner: str,
    now: int,
    uncertain: bool,
    error: str,
) -> bool:
    available_at = now + retry_delay_seconds(job["attempts"]) * 1000
    return Outbox(db).fail_final(
        job["id"], owner, now, error, available_at, uncertain=uncertain
    )


def _default_clock() -> int:
    return int(time.time() * 1000)


def _freeze_unknown(
    db: QueueDB, job_id: int, owner: str, now: int, error: str
) -> None:
    Outbox(db).mark_final_uncertain(job_id, owner, now, error)
    db.recover_expired(now)


def _settle_response(
    db: QueueDB,
    consumer: ConsumerQueue,
    job: Dict[str, Any],
    owner: str,
    response: Any,
    now: int,
) -> str:
    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        _freeze_unknown(db, job["id"], owner, now, "InvalidResponse")
        return "uncertain"
    if status in DEFINITE_REJECTION_STATUSES:
        failed = _fail_after_send_attempt(
            db, job, owner, now, False, "HTTP {}".format(status)
        )
        if failed:
            return "failed"
        _freeze_unknown(db, job["id"], owner, now, "LeaseExpired")
        return "uncertain"
    if not 200 <= status < 300:
        _freeze_unknown(
            db, job["id"], owner, now, "HTTP {}".format(status)
        )
        return "uncertain"
    if consumer.complete_final_pending(job["id"], owner, now):
        return "done"
    _freeze_unknown(db, job["id"], owner, now, "SettlementError")
    return "uncertain"


def send_final(
    db: QueueDB,
    job_id: int,
    owner: str,
    reply: str,
    markdown: bool,
    server: str,
    api_key: str,
    now: Optional[int] = None,
    *,
    sender: Optional[Sender] = None,
    recorder: Optional[Recorder] = None,
    clock: Optional[Clock] = None,
) -> Dict[str, Any]:
    """Persist, send once, settle done, then record locally."""
    active_clock = clock or ((lambda: now) if now is not None else _default_clock)
    prepare_now = active_clock()
    consumer = ConsumerQueue(db)
    job = db.get(job_id)
    record = _reply_record(job, reply, markdown, prepare_now)
    if not consumer.prepare_final(
        job_id, owner, record, prepare_now, FINAL_LEASE_SECONDS
    ):
        return _result(job_id, "blocked")
    try:
        response = _send(sender or _default_sender(), server, api_key, record)
    except Exception:
        outcome_now = active_clock()
        _freeze_unknown(
            db, job_id, owner, outcome_now, "TransportError"
        )
        return _result(job_id, "uncertain")
    outcome_now = active_clock()
    status = _settle_response(
        db, consumer, job, owner, response, outcome_now
    )
    if status != "done":
        return _result(job_id, status)
    active_recorder = recorder or _default_recorder()
    try:
        active_recorder(record)
    except Exception:
        return _result(job_id, "done", True)
    pending = not consumer.mark_recorded(job_id, outcome_now)
    return _result(job_id, "done", pending)


def repair_record(
    db: QueueDB,
    job_id: int,
    now: int,
    *,
    recorder: Optional[Recorder] = None,
) -> bool:
    """Replay only local recording from durable reply material."""
    consumer = ConsumerQueue(db)
    record = consumer.pending_record(job_id)
    if record is None:
        return False
    try:
        (recorder or _default_recorder())(record)
    except Exception:
        return False
    return consumer.mark_recorded(job_id, now)
