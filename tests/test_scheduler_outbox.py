"""事务性通知发送预约的行为测试。"""

import json
import sqlite3

import pytest

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.outbox import Outbox


def make_job(db, conv_id="u1", mid=1):
    """创建一个最小通知任务。"""
    return db.enqueue(
        {"conv_id": conv_id, "mid": mid, "content": "hello"},
        detected_at=100,
    )


def finish_job(db, job_id, owner, now):
    """Settle a job through a staged final record."""
    job = db.get(job_id)
    record = {
        "conv_id": job["conv_id"], "mid": job["mid"], "reply": "done",
        "markdown": False, "bot_uid": 7, "created_at": now,
    }
    consumer = ConsumerQueue(db)
    assert consumer.prepare_final(job_id, owner, record, now - 1, 10)
    return consumer.complete_final_pending(job_id, owner, now)


def test_jobs_only_database_upgrades_without_losing_existing_jobs(tmp_path):
    """旧版仅 jobs 数据库初始化后应保留数据并补齐 outbox 表索引。"""
    path = tmp_path / "legacy.db"
    old_schema = """
    CREATE TABLE jobs (
     id INTEGER PRIMARY KEY AUTOINCREMENT,
     conv_id TEXT NOT NULL,
     mid INTEGER NOT NULL,
     payload_json TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'pending',
     network_mode TEXT NOT NULL DEFAULT 'unknown',
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
    """
    stored = {"conv_id": "u7", "mid": 9, "content": "legacy"}
    with sqlite3.connect(str(path)) as connection:
        connection.executescript(old_schema)
        connection.execute(
            """
            INSERT INTO jobs(
                conv_id,mid,payload_json,detected_at,available_at,
                evidence_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,'[]',?,?)
            """,
            ("u7", 9, json.dumps(stored), 100, 100, 100, 100),
        )

    db = QueueDB(path)
    assert db.get(1)["payload"] == stored
    outbox = Outbox(path)
    assert outbox.claim(1, "ack", "owner", 200, 10)
    with sqlite3.connect(str(path)) as connection:
        objects = dict(connection.execute(
            "SELECT name,type FROM sqlite_master "
            "WHERE name IN ('deliveries','idx_deliveries_state_lease')"
        ))
    assert objects == {
        "deliveries": "table",
        "idx_deliveries_state_lease": "index",
    }


def test_claim_creates_delivery_and_rejects_parallel_kind(tmp_path):
    """同一任务同时只能有一个未结束的发送预约。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)

    assert outbox.claim(job_id, "ack", "owner-a", 200, 10)
    assert not outbox.claim(job_id, "partial", "owner-b", 201, 10)
    assert outbox.state(job_id, "ack") == {
        "job_id": job_id,
        "kind": "ack",
        "state": "claimed",
        "owner": "owner-a",
        "lease_until": 10_200,
        "attempted_at": 200,
        "sent_at": None,
        "last_error": None,
    }


@pytest.mark.parametrize("terminal", ("done", "cancelled"))
def test_claim_rejects_terminal_jobs(tmp_path, terminal):
    """完成或取消的任务不得创建发送预约。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    if terminal == "done":
        db.claim("worker", 100, 1, 10)
        assert finish_job(db, job_id, "worker", 200)
    else:
        assert db.cancel(job_id, 200)

    assert not Outbox(db).claim(job_id, "ack", "notify", 300, 10)


def test_mark_sent_requires_live_owner_and_updates_marker_atomically(tmp_path):
    """发送成功只可由有效预约 owner 记录并同步任务标记。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "ack", "owner", 200, 10)

    assert not outbox.mark_sent(job_id, "ack", "wrong", 300)
    assert not outbox.mark_sent(job_id, "ack", "owner", 10_200)
    assert outbox.mark_sent(job_id, "ack", "owner", 10_199)
    assert outbox.state(job_id, "ack")["state"] == "sent"
    assert db.get(job_id)["ack_sent_at"] == 10_199
    assert not outbox.claim(job_id, "ack", "other", 11_000, 10)


def test_mark_sent_rolls_back_marker_when_delivery_update_affects_zero(tmp_path):
    """delivery 成功更新被忽略时 jobs marker 必须一并回滚。"""
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "ack", "owner", 200, 10)
    with sqlite3.connect(str(path)) as connection:
        connection.executescript(
            """
            CREATE TRIGGER ignore_delivery_sent
            BEFORE UPDATE OF state ON deliveries
            WHEN NEW.state = 'sent'
            BEGIN
              SELECT RAISE(IGNORE);
            END;
            """
        )

    assert not outbox.mark_sent(job_id, "ack", "owner", 300)
    assert db.get(job_id)["ack_sent_at"] is None
    state = outbox.state(job_id, "ack")
    assert state["state"] == "claimed"
    assert state["sent_at"] is None


def test_mark_sent_keeps_delivery_claimed_when_marker_update_affects_zero(
    tmp_path,
):
    """jobs marker 成功更新被忽略时 delivery 必须保持 claimed。"""
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "ack", "owner", 200, 10)
    with sqlite3.connect(str(path)) as connection:
        connection.executescript(
            """
            CREATE TRIGGER ignore_ack_marker
            BEFORE UPDATE OF ack_sent_at ON jobs
            WHEN NEW.ack_sent_at IS NOT NULL
            BEGIN
              SELECT RAISE(IGNORE);
            END;
            """
        )

    assert not outbox.mark_sent(job_id, "ack", "owner", 300)
    assert db.get(job_id)["ack_sent_at"] is None
    state = outbox.state(job_id, "ack")
    assert state["state"] == "claimed"
    assert state["sent_at"] is None


def test_http_failure_can_be_reclaimed_and_sent(tmp_path):
    """明确失败的预约可在下一轮重新领取。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "partial", "first", 200, 10)
    assert outbox.mark_failed(
        job_id, "partial", "first", 300, "HTTP 503", uncertain=False
    )

    assert outbox.state(job_id, "partial")["last_error"] == "HTTP 503"
    assert outbox.claim(job_id, "partial", "second", 400, 10)
    assert outbox.mark_sent(job_id, "partial", "second", 500)
    assert db.get(job_id)["partial_sent_at"] == 500


def test_uncertain_delivery_is_never_reclaimed(tmp_path):
    """结果不确定的预约必须保持最多一次而不自动重发。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "ack", "owner", 200, 10)
    assert outbox.mark_failed(
        job_id, "ack", "owner", 300, "Timeout", uncertain=True
    )

    state = outbox.state(job_id, "ack")
    assert state["state"] == "uncertain"
    assert state["owner"] is None and state["lease_until"] is None
    assert not outbox.claim(job_id, "ack", "other", 400, 10)


def test_expired_claim_becomes_uncertain_without_reclaim(tmp_path):
    """过期预约应转为不确定且当前及后续调用均不重领。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "ack", "owner", 200, 10)

    assert not outbox.claim(job_id, "ack", "other", 10_200, 10)
    assert outbox.state(job_id, "ack")["state"] == "uncertain"
    assert not outbox.claim(job_id, "ack", "third", 20_000, 10)


@pytest.mark.parametrize("partial_state", ("claimed", "sent", "uncertain", "failed"))
def test_any_partial_delivery_permanently_blocks_ack_claim(
    tmp_path, partial_state
):
    """任何阶段通知尝试都应在事务内永久阻止后续确认预约。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "partial", "partial-owner", 200, 10)
    if partial_state == "sent":
        assert outbox.mark_sent(job_id, "partial", "partial-owner", 300)
    elif partial_state in ("uncertain", "failed"):
        assert outbox.mark_failed(
            job_id,
            "partial",
            "partial-owner",
            300,
            "Timeout" if partial_state == "uncertain" else "HTTP 503",
            uncertain=partial_state == "uncertain",
        )

    assert not outbox.claim(job_id, "ack", "ack-owner", 400, 10)
    assert outbox.state(job_id, "ack") is None


@pytest.mark.parametrize("kind", ("bad", "", None))
def test_outbox_rejects_unknown_delivery_kind(tmp_path, kind):
    """预约类型必须受数据库支持范围约束。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)

    with pytest.raises(ValueError):
        Outbox(db).claim(job_id, kind, "owner", 200, 10)


def test_final_claim_requires_same_live_job_owner(tmp_path):
    """正式回复预约必须与任务租约同 owner 且两者均有效。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    assert db.claim("worker", 100, 1, 10)
    outbox = Outbox(db)

    assert not outbox.claim(job_id, "final", "other", 200, 5)
    assert outbox.claim(job_id, "final", "worker", 200, 5)
    assert not outbox.renew(job_id, "final", "other", 300, 5)
    assert outbox.renew(job_id, "final", "worker", 300, 5)
    assert not outbox.renew(job_id, "final", "worker", 10_100, 5)


def test_final_mark_sent_atomically_completes_job(tmp_path):
    """正式回复发送成功记录与任务完成必须处于同一事务。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    assert db.claim("worker", 100, 1, 10)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "final", "worker", 200, 5)

    assert not outbox.mark_sent(job_id, "final", "other", 300)
    assert outbox.mark_sent(job_id, "final", "worker", 300)
    job = db.get(job_id)
    assert job["status"] == "done"
    assert job["final_sent_at"] == 300
    assert outbox.state(job_id, "final")["state"] == "sent"


def test_final_completion_rolls_back_delivery_when_job_update_fails(tmp_path):
    """任务完成写入失败时，正式发送记录也不得单独提交。"""
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = make_job(db)
    assert db.claim("worker", 100, 1, 10)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "final", "worker", 200, 5)
    with sqlite3.connect(str(path)) as connection:
        connection.executescript(
            """
            CREATE TRIGGER ignore_job_done
            BEFORE UPDATE OF status ON jobs
            WHEN NEW.status = 'done'
            BEGIN
              SELECT RAISE(IGNORE);
            END;
            """
        )

    assert not outbox.mark_sent(job_id, "final", "worker", 300)
    assert db.get(job_id)["status"] == "processing"
    assert outbox.state(job_id, "final")["state"] == "claimed"


def test_crash_after_final_claim_blocks_reclaim_and_duplicate_send(tmp_path):
    """正式预约后的崩溃宁可待人工确认，也不得再次发送。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    assert db.claim("first", 100, 1, 1)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "final", "first", 200, 1)

    assert not outbox.claim(job_id, "final", "second", 1_200, 10)
    assert outbox.state(job_id, "final")["state"] == "uncertain"
    assert db.recover_expired(1_200) == 1
    assert db.claim("second", 1_300, 1, 10) == []


def test_explicit_final_failure_returns_job_to_retry(tmp_path):
    """明确未发送的正式回复可原子释放任务并允许安全重试。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    assert db.claim("first", 100, 1, 10)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "final", "first", 200, 5)

    assert not outbox.fail_final(
        job_id, "other", 300, "SendFailed", available_at=500
    )
    assert outbox.fail_final(
        job_id, "first", 300, "SendFailed", available_at=500
    )
    assert db.get(job_id)["status"] == "retry_wait"
    assert outbox.state(job_id, "final")["state"] == "failed"
    assert db.claim("second", 500, 1, 10)
    assert outbox.claim(job_id, "final", "second", 600, 5)


@pytest.mark.parametrize("kind", ("ack", "partial"))
def test_mark_failed_requires_live_matching_claim_owner(tmp_path, kind):
    """通知失败只可由有效预约 owner 在租约内记录。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, kind, "owner", 200, 1)

    assert not outbox.mark_failed(
        job_id, kind, "wrong", 300, "HTTP 503"
    )
    assert outbox.state(job_id, kind)["state"] == "claimed"


@pytest.mark.parametrize("kind", ("ack", "partial", "final"))
def test_expired_failure_becomes_uncertain_and_never_reclaims(
    tmp_path, kind
):
    """预约在边界过期后不得降为 failed 并重新开放发送。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    if kind == "final":
        assert db.claim("owner", 100, 1, 10)
    outbox = Outbox(db)
    assert outbox.claim(job_id, kind, "owner", 200, 1)

    if kind == "final":
        changed = outbox.fail_final(
            job_id, "owner", 1_200, "HTTP 503", available_at=2_000
        )
    else:
        changed = outbox.mark_failed(
            job_id, kind, "owner", 1_200, "HTTP 503"
        )

    assert not changed
    assert outbox.state(job_id, kind)["state"] == "uncertain"
    assert not outbox.claim(job_id, kind, "next", 1_300, 10)


def test_unknown_error_identifier_is_stored_as_internal_error(tmp_path):
    """Outbox never persists arbitrary caller-provided identifiers."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "ack", "owner", 200, 10)

    assert outbox.mark_failed(
        job_id, "ack", "owner", 300, "PrivateCustomerIdentifier"
    )

    assert outbox.state(job_id, "ack")["last_error"] == "InternalError"
