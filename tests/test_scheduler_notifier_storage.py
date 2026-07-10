"""通知发送持久化失败与重试的行为测试。"""

import pytest

from scheduler.db import QueueDB
from scheduler.notifier import process_due_notifications
from scheduler.outbox import Outbox


class Response:
    """提供发送测试所需的最小 HTTP 响应。"""

    def __init__(self, ok=True, status_code=200):
        self.ok = ok
        self.status_code = status_code


def payload(conv_id, mid=1):
    """构造通知任务载荷。"""
    return {"conv_id": conv_id, "mid": mid, "content": "hello"}


def test_failed_partial_retries_partial_but_never_falls_back_to_ack(tmp_path):
    """阶段通知明确失败后只应重试阶段通知而不降级为确认。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u1"), detected_at=100)
    calls = []

    def sender(*args, **kwargs):
        calls.append(1)
        return Response(ok=len(calls) > 1, status_code=503)

    first = process_due_notifications(
        db, "server", "key", 45_100, sender=sender
    )
    assert first.failed == 1 and first.sent == 0
    assert len(calls) == 1
    assert Outbox(db).state(job_id, "partial")["state"] == "failed"
    assert Outbox(db).state(job_id, "ack") is None

    second = process_due_notifications(
        db, "server", "key", 45_101, sender=sender
    )
    assert second.sent == 1
    assert len(calls) == 2
    assert db.get(job_id)["partial_sent_at"] == 45_101
    assert Outbox(db).state(job_id, "ack") is None


def test_http_error_records_only_status_and_retries(tmp_path):
    """明确 HTTP 失败只记录状态码并允许下一轮重试。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u1"), detected_at=100)
    responses = [Response(ok=False, status_code=503), Response()]

    first = process_due_notifications(
        db, "secret-server", "secret-key", 10_100,
        sender=lambda *args, **kwargs: responses.pop(0),
    )
    assert first.failed == 1 and first.sent == 0
    state = Outbox(db).state(job_id, "ack")
    assert state["state"] == "failed"
    assert state["last_error"] == "HTTP 503"
    assert "secret" not in state["last_error"]

    second = process_due_notifications(
        db, "secret-server", "secret-key", 10_101,
        sender=lambda *args, **kwargs: responses.pop(0),
    )
    assert second.sent == 1
    assert db.get(job_id)["ack_sent_at"] == 10_101


@pytest.mark.parametrize("failure_mode", ("false", "raise"))
def test_http_failure_storage_error_is_not_reported_as_failed(
    tmp_path, monkeypatch, failure_mode
):
    """明确发送失败仅在状态成功落库后才计入 failed。"""
    db = QueueDB(tmp_path / "queue.db")
    db.enqueue(payload("u1"), detected_at=100)

    def broken_mark(*args, **kwargs):
        if failure_mode == "raise":
            raise RuntimeError("database unavailable")
        return False

    monkeypatch.setattr(Outbox, "mark_failed", broken_mark)
    stats = process_due_notifications(
        db, "server", "key", 10_100,
        sender=lambda *args, **kwargs: Response(False, 503),
    )

    assert stats.failed == 0
    assert stats.uncertain == 0
    assert stats.storage_errors == 1


@pytest.mark.parametrize("failure_mode", ("false", "raise"))
def test_uncertain_storage_error_is_not_reported_as_uncertain(
    tmp_path, monkeypatch, failure_mode
):
    """发送结果不确定仅在状态成功落库后才计入 uncertain。"""
    db = QueueDB(tmp_path / "queue.db")
    db.enqueue(payload("u1"), detected_at=100)

    def broken_mark(*args, **kwargs):
        if failure_mode == "raise":
            raise RuntimeError("database unavailable")
        return False

    monkeypatch.setattr(Outbox, "mark_failed", broken_mark)

    def sender(*args, **kwargs):
        raise TimeoutError("secret details")

    stats = process_due_notifications(
        db, "server", "key", 10_100, sender=sender
    )

    assert stats.failed == 0
    assert stats.uncertain == 0
    assert stats.storage_errors == 1


def test_claim_exception_is_storage_error_not_skip(tmp_path, monkeypatch):
    """预约持久化异常应计存储错误而非普通跳过。"""
    db = QueueDB(tmp_path / "queue.db")
    db.enqueue(payload("u1"), detected_at=100)
    calls = []

    def broken_claim(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(Outbox, "claim", broken_claim)
    stats = process_due_notifications(
        db, "server", "key", 10_100,
        sender=lambda *args, **kwargs: calls.append(1),
    )

    assert stats.storage_errors == 1
    assert stats.skipped == 0
    assert calls == []


def test_mark_sent_and_fallback_failures_are_both_counted(
    tmp_path, monkeypatch
):
    """成功标记及其不确定回退都失败时应分别计存储错误。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload("u1"), detected_at=100)
    monkeypatch.setattr(Outbox, "mark_sent", lambda *args, **kwargs: False)
    monkeypatch.setattr(Outbox, "mark_failed", lambda *args, **kwargs: False)

    stats = process_due_notifications(
        db, "server", "key", 10_100,
        sender=lambda *args, **kwargs: Response(),
    )

    assert stats.sent == 0 and stats.uncertain == 0
    assert stats.storage_errors == 2
    assert Outbox(db).state(job_id, "ack")["state"] == "claimed"


def test_job_read_and_cleanup_failures_are_both_counted(
    tmp_path, monkeypatch
):
    """预约后任务读取及清理都失败时应分别计存储错误。"""
    db = QueueDB(tmp_path / "queue.db")
    db.enqueue(payload("u1"), detected_at=100)

    def broken_get(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "get", broken_get)
    monkeypatch.setattr(Outbox, "mark_failed", lambda *args, **kwargs: False)
    stats = process_due_notifications(
        db, "server", "key", 10_100,
        sender=lambda *args, **kwargs: Response(),
    )

    assert stats.storage_errors == 2
    assert stats.sent == stats.failed == stats.uncertain == 0


def test_failure_recording_exception_does_not_block_other_job(
    tmp_path, monkeypatch
):
    """明确失败落库异常时记录存储错误并继续后续任务。"""
    db = QueueDB(tmp_path / "queue.db")
    broken = db.enqueue(payload("u1", 1), detected_at=100)
    good = db.enqueue(payload("u2", 2), detected_at=100)
    original_mark_failed = Outbox.mark_failed

    def mark_failed(outbox, job_id, *args, **kwargs):
        if job_id == broken:
            raise RuntimeError("database unavailable")
        return original_mark_failed(outbox, job_id, *args, **kwargs)

    def sender(server, key, text, **kwargs):
        return Response(ok=kwargs["uid"] != 1, status_code=503)

    monkeypatch.setattr(Outbox, "mark_failed", mark_failed)
    stats = process_due_notifications(
        db, "server", "key", 10_100, sender=sender
    )

    assert stats.failed == 0
    assert stats.storage_errors == 1
    assert stats.sent == 1
    assert Outbox(db).state(broken, "ack")["state"] == "claimed"
    assert db.get(good)["ack_sent_at"] == 10_100


def test_sender_exception_is_uncertain_and_other_job_continues(tmp_path):
    """发送异常成功落库为 uncertain 且不阻断后续任务。"""
    db = QueueDB(tmp_path / "queue.db")
    uncertain = db.enqueue(payload("u1", 1), detected_at=100)
    good = db.enqueue(payload("u2", 2), detected_at=100)
    calls = []

    def sender(server, key, text, **kwargs):
        target = kwargs["uid"]
        calls.append(target)
        if target == 1:
            raise TimeoutError("contains secret payload")
        return Response()

    stats = process_due_notifications(
        db, "server", "key", 10_100, sender=sender
    )
    assert stats.sent == 1 and stats.uncertain == 1
    assert stats.storage_errors == 0
    state = Outbox(db).state(uncertain, "ack")
    assert state["state"] == "uncertain"
    assert state["last_error"] == "InternalError"
    assert db.get(good)["ack_sent_at"] == 10_100

    assert process_due_notifications(
        db, "server", "key", 10_101, sender=sender
    ).sent == 0
    assert calls == [1, 2]
