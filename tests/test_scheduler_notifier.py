"""占位、阶段和状态通知的行为测试。"""

import builtins
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.notifier import (
    NotificationStats,
    process_due_notifications,
    render_ack,
    render_partial,
    render_status,
    send_notification,
    target_from_conv,
)
from scheduler.outbox import Outbox


class Response:
    """提供发送测试所需的最小 HTTP 响应。"""
    def __init__(self, ok=True, status_code=200):
        self.ok = ok
        self.status_code = status_code


def payload(conv_id, mid=1):
    """构造通知任务载荷。"""
    return {"conv_id": conv_id, "mid": mid, "content": "hello"}


def finish_job(db, job_id, owner, now):
    """Settle a job through its final outbox transaction."""
    job = db.get(job_id)
    record = {
        "conv_id": job["conv_id"], "mid": job["mid"], "reply": "done",
        "markdown": False, "bot_uid": 7, "created_at": now,
    }
    consumer = ConsumerQueue(db)
    assert consumer.prepare_final(job_id, owner, record, now - 1, 10)
    return consumer.complete_final_pending(job_id, owner, now)

@pytest.mark.parametrize(
    "conv_id,expected",
    (("u2", {"uid": 2}), ("g5", {"gid": 5}), ("u001", {"uid": 1})),
)
def test_target_from_conv_accepts_positive_user_and_group_ids(conv_id, expected):
    """合法会话 ID 应转换为发送目标。"""
    assert target_from_conv(conv_id) == expected


@pytest.mark.parametrize(
    "conv_id",
    (None, "", "u", "g", "u0", "g-1", "x2", "u2x", 2, True),
)
def test_target_from_conv_rejects_invalid_ids(conv_id):
    """空、未知或非正整数会话 ID 应被拒绝。"""
    with pytest.raises(ValueError):
        target_from_conv(conv_id)


def test_ack_and_status_text_are_explicit():
    """确认和状态文本应准确表达当前进度。"""
    assert render_ack() == "已收到，正在处理，稍后给你完整回复。"
    status = render_status()
    assert "排队" in status or "查询" in status
    assert "完成" in status
    assert "补充" in status


def test_render_partial_limits_items_summary_and_sanitizes_content():
    """阶段通知应限制内容并移除 HTML 与控制字符。"""
    evidence = [
        {
            "source": "<b> API </b> &lt;img src=x",
            "title": "  First\n title ",
            "summary": "x" * 170 + "\x00<script>alert(1)</script>",
        },
        {"source": " cache ", "title": "Second", "summary": " useful\ttext "},
        {"source": "web", "title": "Third", "summary": "third"},
        {"source": "extra", "title": "Fourth", "summary": "must not appear"},
    ]

    text = render_partial(evidence)

    assert "仍在补充" in text
    assert "Fourth" not in text
    assert "<" not in text and ">" not in text and "\x00" not in text
    assert "First title" in text and "useful text" in text
    assert "x" * 160 in text
    assert "x" * 161 not in text


def test_render_partial_safely_degrades_missing_or_non_text_fields():
    """缺失字段或异常字段类型不应破坏阶段通知。"""
    text = render_partial([{}, {"source": None, "title": 3, "summary": ["bad"]}])

    assert isinstance(text, str)
    assert "仍在补充" in text
    assert "None" not in text
    assert "['bad']" not in text


@pytest.mark.parametrize(
    "conv_id,target",
    (("u2", {"uid": 2}), ("g5", {"gid": 5})),
)
def test_send_notification_passes_exact_sender_arguments(conv_id, target):
    """发送包装应准确转交目标、格式和超时。"""
    calls = []

    def sender(server, key, text, **kwargs):
        calls.append((server, key, text, kwargs))
        return Response()

    response = send_notification(
        "https://chat", "secret", conv_id, "hello",
        markdown=True, timeout=7, sender=sender,
    )

    assert response.ok
    assert calls == [
        ("https://chat", "secret", "hello",
         dict(target, markdown=True, timeout=7))
    ]


def test_ack_success_marks_once_and_does_not_touch_state_or_history(tmp_path):
    """十秒确认成功后应记录标记且重复轮询不重发。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u2"), detected_at=100)
    calls = []
    before = set(os.listdir(str(tmp_path)))

    def sender(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    assert process_due_notifications(
        db, "server", "key", 10_100, sender=sender
    ).sent == 1
    assert process_due_notifications(
        db, "server", "key", 10_101, sender=sender
    ).sent == 0
    assert db.get(job_id)["ack_sent_at"] == 10_100
    assert len(calls) == 1
    assert set(os.listdir(str(tmp_path))) == before


def test_process_with_injected_sender_cannot_write_paths_or_import_reply(
    tmp_path, monkeypatch
):
    """通知处理不得写文件或导入正式回复入口。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u2"), detected_at=100)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"reply_and_record", "scripts.reply_and_record"}:
            raise AssertionError("must not import reply_and_record")
        return original_import(name, *args, **kwargs)

    def reject_path_write(*args, **kwargs):
        raise AssertionError("must not write state or history paths")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(Path, "write_text", reject_path_write)
    monkeypatch.setattr(Path, "open", reject_path_write)

    assert process_due_notifications(
        db, "server", "key", 10_100,
        sender=lambda *args, **kwargs: Response(),
    ).sent == 1
    assert db.get(job_id)["ack_sent_at"] == 10_100


@pytest.mark.parametrize("with_evidence", (True, False))
def test_partial_or_status_precedes_ack_and_marks_job(tmp_path, with_evidence):
    """四十五秒阶段通知应优先发送，之后不得补发确认。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("g5"), detected_at=100)
    if with_evidence:
        db._append_evidence_unchecked(
            job_id,
            {"source": "api", "title": "result", "summary": "details"},
            now=200,
        )
    texts = []

    def sender(server, key, text, **kwargs):
        texts.append(text)
        return Response()

    assert process_due_notifications(
        db, "server", "key", 45_100, sender=sender
    ).sent == 1
    job = db.get(job_id)
    assert job["partial_sent_at"] == 45_100
    assert job["ack_sent_at"] is None
    assert ("result" in texts[0]) is with_evidence
    assert process_due_notifications(
        db, "server", "key", 45_101, sender=sender
    ).sent == 0


def test_no_evidence_sends_exact_rendered_status(tmp_path):
    """无证据的四十五秒分支应发送精确状态文本。"""
    db = QueueDB(tmp_path / "queue.db")
    db.enqueue(payload("u1"), detected_at=100)
    texts = []

    def sender(server, key, text, **kwargs):
        texts.append(text)
        return Response()

    assert process_due_notifications(
        db, "server", "key", 45_100, sender=sender
    ).sent == 1
    assert texts == [render_status()]


def test_partial_failures_continue_and_retry_next_round(tmp_path):
    """阶段通知非成功响应和异常应保留标记并允许下轮重试。"""
    db = QueueDB(tmp_path / "queue.db")
    failed = db.enqueue(payload("u1", 1), detected_at=100)
    raised = db.enqueue(payload("g2", 2), detected_at=100)
    good = db.enqueue(payload("u3", 3), detected_at=100)
    for job_id, title in ((failed, "failed"), (raised, "raised"), (good, "good")):
        db._append_evidence_unchecked(
            job_id,
            {"source": "api", "title": title, "summary": "details"},
            now=200,
        )
    retrying = {1, 2}
    texts = {}

    def sender(server, key, text, **kwargs):
        target = kwargs.get("uid") or kwargs.get("gid")
        texts.setdefault(target, []).append(text)
        if target in retrying:
            if target == 1:
                return Response(ok=False)
            raise RuntimeError("network")
        return Response()

    first = process_due_notifications(
        db, "server", "key", 45_100, sender=sender
    )
    assert first.sent == 1
    assert first.failed == 1
    assert first.uncertain == 1
    assert first.skipped == 2
    assert db.get(failed)["partial_sent_at"] is None
    assert db.get(raised)["partial_sent_at"] is None
    assert db.get(good)["partial_sent_at"] == 45_100
    assert "failed" in texts[1][0]
    assert "raised" in texts[2][0]
    assert "good" in texts[3][0]

    retrying.clear()
    assert process_due_notifications(
        db, "server", "key", 45_101, sender=sender
    ).sent == 1
    assert db.get(failed)["partial_sent_at"] == 45_101
    assert db.get(raised)["partial_sent_at"] is None


def test_status_failures_continue_and_retry_next_round(tmp_path):
    """状态通知非成功响应和异常应保留标记并允许下轮重试。"""
    db = QueueDB(tmp_path / "queue.db")
    failed = db.enqueue(payload("u1", 1), detected_at=100)
    raised = db.enqueue(payload("g2", 2), detected_at=100)
    good = db.enqueue(payload("u3", 3), detected_at=100)
    retrying = {1, 2}
    texts = {}

    def sender(server, key, text, **kwargs):
        target = kwargs.get("uid") or kwargs.get("gid")
        texts.setdefault(target, []).append(text)
        if target in retrying:
            if target == 1:
                return Response(ok=False)
            raise RuntimeError("network")
        return Response()

    first = process_due_notifications(
        db, "server", "key", 45_100, sender=sender
    )
    assert first.sent == 1
    assert first.failed == 1
    assert first.uncertain == 1
    assert first.skipped == 2
    assert db.get(failed)["partial_sent_at"] is None
    assert db.get(raised)["partial_sent_at"] is None
    assert db.get(good)["partial_sent_at"] == 45_100
    assert all(text == render_status() for text in texts[3])

    retrying.clear()
    assert process_due_notifications(
        db, "server", "key", 45_101, sender=sender
    ).sent == 1
    assert db.get(failed)["partial_sent_at"] == 45_101
    assert db.get(raised)["partial_sent_at"] is None
    assert texts[1][0] == texts[1][-1] == render_status()
    assert texts[2][0] == render_status()


def test_failures_are_retryable_and_do_not_block_other_jobs(tmp_path):
    """HTTP 失败、异常和坏会话均不得标记或阻断其余任务。"""
    db = QueueDB(tmp_path / "queue.db")
    failed = db.enqueue(payload("u1", 1), detected_at=100)
    raised = db.enqueue(payload("g2", 2), detected_at=100)
    invalid = db.enqueue(payload("bad", 3), detected_at=100)
    good = db.enqueue(payload("u4", 4), detected_at=100)
    attempts = {}

    def sender(server, key, text, **kwargs):
        target = kwargs.get("uid") or kwargs.get("gid")
        attempts[target] = attempts.get(target, 0) + 1
        if target == 1 and attempts[target] == 1:
            return Response(ok=False)
        if target == 2 and attempts[target] == 1:
            raise RuntimeError("network")
        return Response()

    first = process_due_notifications(
        db, "server", "key", 10_100, sender=sender
    )
    assert first == NotificationStats(sent=1, failed=1, uncertain=2, skipped=0)
    assert db.get(failed)["ack_sent_at"] is None
    assert db.get(raised)["ack_sent_at"] is None
    assert db.get(invalid)["ack_sent_at"] is None
    assert db.get(good)["ack_sent_at"] == 10_100

    assert process_due_notifications(
        db, "server", "key", 10_101, sender=sender
    ).sent == 1
    assert db.get(failed)["ack_sent_at"] == 10_101
    assert db.get(raised)["ack_sent_at"] is None
    assert db.get(invalid)["ack_sent_at"] is None


def test_done_and_cancelled_jobs_are_not_notified(tmp_path):
    """完成和取消的任务不应发送任何通知。"""
    db = QueueDB(tmp_path / "queue.db")
    done = db.enqueue(payload("u1", 1), detected_at=100)
    cancelled = db.enqueue(payload("g2", 2), detected_at=100)
    db.claim("owner", now=100, limit=2, lease_seconds=100)
    assert finish_job(db, done, "owner", 200)
    assert db.cancel(cancelled, now=200)
    calls = []

    assert process_due_notifications(
        db, "server", "key", 100_000,
        sender=lambda *args, **kwargs: calls.append(1),
    ).sent == 0
    assert calls == []


def test_concurrent_process_calls_send_same_ack_once(tmp_path, monkeypatch):
    """同一到期快照上的两个处理轮次只能实际发送一次确认。"""
    db = QueueDB(tmp_path / "queue.db")
    db.enqueue(payload("u1"), detected_at=100)
    barrier = Barrier(2)
    calls = []
    original_due = QueueDB.due_for_ack

    def synchronized_due(worker_db, now, delay_ms):
        jobs = original_due(worker_db, now, delay_ms)
        barrier.wait(timeout=5)
        return jobs

    def sender(*args, **kwargs):
        calls.append(1)
        return Response()

    def process(_):
        return process_due_notifications(
            QueueDB(db.path), "server", "key", 10_100, sender=sender
        )

    monkeypatch.setattr(QueueDB, "due_for_ack", synchronized_due)
    with ThreadPoolExecutor(max_workers=2) as executor:
        stats = list(executor.map(process, range(2)))

    assert len(calls) == 1
    assert sum(item.sent for item in stats) == 1


@pytest.mark.parametrize("terminal", ("done", "cancelled"))
def test_terminal_race_after_due_query_does_not_send(
    tmp_path, monkeypatch, terminal
):
    """到期查询后任务结束时预约应拒绝且不得发送。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u1"), detected_at=100)
    if terminal == "done":
        db.claim("worker", now=100, limit=1, lease_seconds=100)
    original_due = db.due_for_ack

    def due_then_finish(now, delay_ms):
        jobs = original_due(now, delay_ms)
        if terminal == "done":
            assert finish_job(db, job_id, "worker", 200)
        else:
            assert db.cancel(job_id, now=200)
        return jobs

    monkeypatch.setattr(db, "due_for_ack", due_then_finish)
    calls = []
    stats = process_due_notifications(
        db, "server", "key", 10_100,
        sender=lambda *args, **kwargs: calls.append(1),
    )

    assert calls == []
    assert stats == NotificationStats(sent=0, failed=0, uncertain=0, skipped=1)
    assert Outbox(db).state(job_id, "ack") is None


@pytest.mark.parametrize("failure_mode", ("false", "raise"))
def test_mark_sent_failure_is_uncertain_and_not_counted_or_retried(
    tmp_path, monkeypatch, failure_mode
):
    """发送后落库失败应视为不确定且不得立即重复发送。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u1"), detected_at=100)
    calls = []

    def broken_mark(*args, **kwargs):
        if failure_mode == "raise":
            raise RuntimeError("database unavailable")
        return False

    monkeypatch.setattr(Outbox, "mark_sent", broken_mark)
    sender = lambda *args, **kwargs: calls.append(1) or Response()

    stats = process_due_notifications(
        db, "server", "key", 10_100, sender=sender
    )
    assert stats.sent == 0 and stats.uncertain == 0
    assert stats.storage_errors == 1
    assert Outbox(db).state(job_id, "ack")["state"] == "uncertain"
    assert process_due_notifications(
        db, "server", "key", 10_101, sender=sender
    ).sent == 0
    assert len(calls) == 1


def test_uncertain_partial_after_mark_failure_permanently_blocks_ack(
    tmp_path, monkeypatch
):
    """阶段通知已发送但落库失败时不得同轮或后续补发确认。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u1"), detected_at=100)
    original_mark_sent = Outbox.mark_sent
    calls = []

    def mark_sent(outbox, claimed_job, kind, owner, sent_at):
        if kind == "partial":
            return False
        return original_mark_sent(
            outbox, claimed_job, kind, owner, sent_at
        )

    def sender(*args, **kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(Outbox, "mark_sent", mark_sent)
    stats = process_due_notifications(
        db, "server", "key", 45_100, sender=sender
    )

    assert stats.sent == 0 and stats.uncertain == 0
    assert stats.storage_errors == 1
    assert len(calls) == 1
    assert Outbox(db).state(job_id, "partial")["state"] == "uncertain"
    assert Outbox(db).state(job_id, "ack") is None
    process_due_notifications(db, "server", "key", 45_101, sender=sender)
    assert len(calls) == 1
    assert Outbox(db).state(job_id, "ack") is None


def test_uncertain_partial_sender_exception_permanently_blocks_ack(tmp_path):
    """阶段通知发送异常后不得同轮或后续补发确认。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u1"), detected_at=100)
    calls = []

    def sender(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("network")

    stats = process_due_notifications(
        db, "server", "key", 45_100, sender=sender
    )

    assert stats.uncertain == 1
    assert len(calls) == 1
    assert Outbox(db).state(job_id, "partial")["state"] == "uncertain"
    assert Outbox(db).state(job_id, "ack") is None
    process_due_notifications(db, "server", "key", 45_101, sender=sender)
    assert len(calls) == 1
    assert Outbox(db).state(job_id, "ack") is None


