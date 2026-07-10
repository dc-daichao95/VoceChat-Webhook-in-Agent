"""发送不推进正式回复游标的确认、阶段和状态通知。"""

from __future__ import annotations

import html
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from scheduler.outbox import Outbox


ACK_DELAY_MS = 10_000
PARTIAL_DELAY_MS = 45_000
DELIVERY_LEASE_SECONDS = 30
ACK_TEXT = "已收到，正在处理，稍后给你完整回复。"
STATUS_TEXT = "任务仍在排队或查询中，完成后会继续补充完整回复。"
_TAG = re.compile(r"<[^>]*>")


@dataclass
class NotificationStats:
    """汇总一轮通知处理的可观测结果。"""

    sent: int = 0
    failed: int = 0
    uncertain: int = 0
    skipped: int = 0
    storage_errors: int = 0


def target_from_conv(conv_id: str) -> Dict[str, int]:
    """把严格的 uN 或 gN 会话标识转换为发送目标。"""
    if not isinstance(conv_id, str):
        raise ValueError("conversation id must be text")
    match = re.fullmatch(r"([ug])([0-9]+)", conv_id)
    if match is None:
        raise ValueError("invalid conversation id")
    target_id = int(match.group(2))
    if target_id <= 0:
        raise ValueError("conversation target must be positive")
    return {"uid" if match.group(1) == "u" else "gid": target_id}


def render_ack() -> str:
    """返回固定的十秒确认通知。"""
    return ACK_TEXT


def render_status() -> str:
    """返回说明仍在处理且稍后补充的状态通知。"""
    return STATUS_TEXT


def _clean(value: Any, fallback: str, limit: Optional[int] = None) -> str:
    if not isinstance(value, str):
        return fallback
    text = html.unescape(value)
    text = _TAG.sub(" ", text)
    text = text.replace("<", " ").replace(">", " ")
    text = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in text
    )
    text = " ".join(text.split())
    if not text:
        return fallback
    return text if limit is None else text[:limit]


def render_partial(evidence: List[Any]) -> str:
    """把最多三项证据安全渲染为仍待补充的阶段通知。"""
    lines = ["阶段进展："]
    items = evidence if isinstance(evidence, list) else []
    for number, raw_item in enumerate(items[:3], 1):
        item = raw_item if isinstance(raw_item, dict) else {}
        source = _clean(item.get("source"), "来源未知")
        title = _clean(item.get("title"), "未命名条目")
        summary = _clean(item.get("summary"), "暂无摘要", limit=160)
        lines.append(
            "{}. 来源：{}；标题：{}；摘要：{}".format(
                number, source, title, summary
            )
        )
    if len(lines) == 1:
        lines.append("暂未获得可展示的证据。")
    lines.append("仍在补充信息，完成后会给你完整回复。")
    return "\n".join(lines)


def send_notification(
    server: str,
    api_key: str,
    conv_id: str,
    text: str,
    markdown: bool = False,
    timeout: int = 30,
    sender: Optional[Callable[..., Any]] = None,
) -> Any:
    """解析会话目标、发送通知并返回发送器响应。"""
    if sender is None:
        from send import send_message

        sender = send_message
    target = target_from_conv(conv_id)
    return sender(
        server, api_key, text, markdown=markdown, timeout=timeout, **target
    )


def _persist_failure(
    outbox: Outbox,
    job_id: int,
    kind: str,
    owner: str,
    now_ms: int,
    error: str,
    uncertain: bool,
) -> bool:
    try:
        return bool(outbox.mark_failed(
            job_id, kind, owner, now_ms, error, uncertain=uncertain
        ))
    except Exception:
        return False


def _record_send_exception(
    outbox: Outbox,
    job: Dict[str, Any],
    kind: str,
    owner: str,
    now_ms: int,
    error: BaseException,
    stats: NotificationStats,
) -> None:
    if _persist_failure(
        outbox, job["id"], kind, owner, now_ms,
        type(error).__name__, uncertain=True,
    ):
        stats.uncertain += 1
    else:
        stats.storage_errors += 1


def _record_http_failure(
    outbox: Outbox,
    job: Dict[str, Any],
    kind: str,
    owner: str,
    now_ms: int,
    status: Any,
    stats: NotificationStats,
) -> None:
    if _persist_failure(
        outbox, job["id"], kind, owner, now_ms,
        "HTTP {}".format(status), uncertain=False,
    ):
        stats.failed += 1
    else:
        stats.storage_errors += 1


def _send_claimed(
    outbox: Outbox,
    job: Dict[str, Any],
    kind: str,
    owner: str,
    server: str,
    api_key: str,
    now_ms: int,
    text: str,
    sender: Optional[Callable[..., Any]],
    stats: NotificationStats,
) -> None:
    try:
        response = send_notification(
            server, api_key, job["conv_id"], text, sender=sender
        )
    except Exception as error:
        _record_send_exception(
            outbox, job, kind, owner, now_ms, error, stats
        )
        return
    if not getattr(response, "ok", False):
        status = getattr(response, "status_code", 0)
        _record_http_failure(
            outbox, job, kind, owner, now_ms, status, stats
        )
        return
    try:
        marked = outbox.mark_sent(job["id"], kind, owner, now_ms)
    except Exception as error:
        marked = False
        error_name = type(error).__name__
    else:
        error_name = "MarkSentFailed"
    if marked:
        stats.sent += 1
        return
    stats.storage_errors += 1
    if not _persist_failure(
        outbox, job["id"], kind, owner, now_ms,
        error_name, uncertain=True,
    ):
        stats.storage_errors += 1


def _process_job(
    db: Any,
    outbox: Outbox,
    job: Dict[str, Any],
    kind: str,
    owner: str,
    server: str,
    api_key: str,
    now_ms: int,
    text: str,
    sender: Optional[Callable[..., Any]],
    stats: NotificationStats,
) -> None:
    try:
        claimed = outbox.claim(
            job["id"], kind, owner, now_ms, DELIVERY_LEASE_SECONDS
        )
    except Exception:
        stats.storage_errors += 1
        return
    if not claimed:
        stats.skipped += 1
        return
    try:
        current = db.get(job["id"])
    except Exception as error:
        persisted = _persist_failure(
            outbox, job["id"], kind, owner, now_ms,
            type(error).__name__, uncertain=True,
        )
        stats.storage_errors += 1
        if not persisted:
            stats.storage_errors += 1
        return
    if current["status"] in ("done", "cancelled"):
        if _persist_failure(
            outbox, job["id"], kind, owner, now_ms,
            "TerminalRace", uncertain=True,
        ):
            stats.skipped += 1
        else:
            stats.storage_errors += 1
        return
    _send_claimed(
        outbox, current, kind, owner, server, api_key,
        now_ms, text, sender, stats,
    )


def process_due_notifications(
    db: Any,
    server: str,
    api_key: str,
    now_ms: int,
    sender: Optional[Callable[..., Any]] = None,
) -> NotificationStats:
    """事务性预约并发送到期通知，返回结构化处理统计。"""
    stats = NotificationStats()
    outbox = Outbox(db)
    owner = uuid.uuid4().hex
    for job in db.due_for_partial(now_ms, PARTIAL_DELAY_MS):
        text = render_partial(job["evidence"]) if job["evidence"] else render_status()
        _process_job(
            db, outbox, job, "partial", owner, server, api_key,
            now_ms, text, sender, stats,
        )
    for job in db.due_for_ack(now_ms, ACK_DELAY_MS):
        if job.get("partial_sent_at") is not None:
            stats.skipped += 1
            continue
        _process_job(
            db, outbox, job, "ack", owner, server, api_key,
            now_ms, render_ack(), sender, stats,
        )
    return stats
