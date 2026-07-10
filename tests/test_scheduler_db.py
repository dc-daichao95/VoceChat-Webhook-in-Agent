"""Behavior tests for the durable SQLite scheduler queue."""

import sqlite3

from scheduler.db import QueueDB


def payload(mid=1431, conv_id="u2"):
    """Build a minimal inbound message payload."""
    return {
        "mid": mid,
        "conv_id": conv_id,
        "direction": "in",
        "content": "查天气",
    }


def test_enqueue_is_idempotent_and_preserves_first_detection(tmp_path):
    db = QueueDB(tmp_path / "queue.db")

    first = db.enqueue(payload(), detected_at=1000)
    second = db.enqueue(payload(), detected_at=2000)

    assert first == second
    assert db.get(first)["detected_at"] == 1000
    assert db.find("u2", 1431)["payload"] == payload()


def test_schema_enables_wal_and_creates_claim_index(tmp_path):
    path = tmp_path / "queue.db"
    QueueDB(path)

    with sqlite3.connect(str(path)) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        indexes = connection.execute("PRAGMA index_list(jobs)").fetchall()

    assert journal_mode.lower() == "wal"
    assert any(row[1] == "idx_jobs_available" for row in indexes)


def test_claim_is_fifo_and_does_not_skip_blocking_job(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    first = db.enqueue(payload(10), detected_at=1000)
    second = db.enqueue(payload(11), detected_at=1001)

    claimed = db.claim("cursor-a", now=1010, limit=3, lease_seconds=60)
    blocked = db.claim("cursor-b", now=1011, limit=3, lease_seconds=60)

    assert [job["id"] for job in claimed] == [first]
    assert blocked == []
    assert db.get(second)["status"] == "pending"


def test_claim_takes_different_conversations_up_to_limit(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    expected = [
        db.enqueue(payload(1, "u1"), detected_at=1001),
        db.enqueue(payload(2, "u2"), detected_at=1002),
    ]
    db.enqueue(payload(3, "u3"), detected_at=1003)

    claimed = db.claim("cursor-a", now=1010, limit=2, lease_seconds=30)

    assert [job["id"] for job in claimed] == expected
    assert {job["conv_id"] for job in claimed} == {"u1", "u2"}
    assert all(job["attempts"] == 1 for job in claimed)


def test_claim_does_not_skip_unavailable_earlier_job(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    first = db.enqueue(payload(1), detected_at=1000)
    second = db.enqueue(payload(2), detected_at=1001)
    claimed = db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)
    assert [job["id"] for job in claimed] == [first]
    assert db.fail(first, "cursor-a", "wait", available_at=1100)

    assert db.claim("cursor-b", now=1050, limit=1, lease_seconds=10) == []
    assert db.get(second)["status"] == "pending"


def test_renew_requires_processing_job_and_matching_owner(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)

    assert db.renew(job_id, "cursor-b", now=1011, lease_seconds=20) is False
    assert db.renew(job_id, "cursor-a", now=1011, lease_seconds=20) is True
    assert db.get(job_id)["lease_until"] == 1031


def test_expired_processing_lease_returns_to_retry_wait(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)

    assert db.recover_expired(now=1020) == 0
    assert db.recover_expired(now=1021) == 1
    job = db.get(job_id)
    assert job["status"] == "retry_wait"
    assert job["available_at"] == 1021
    assert job["lease_owner"] is None


def test_reply_markers_are_idempotent_and_due_queries_stop_returning_them(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    first = db.enqueue(payload(1, "u1"), detected_at=1000)
    second = db.enqueue(payload(2, "u2"), detected_at=1000)

    assert [job["id"] for job in db.due_for_ack(1010, 10)] == [first, second]
    assert db.mark_ack_sent(first, sent_at=1010) is True
    assert db.mark_ack_sent(first, sent_at=1011) is False
    assert [job["id"] for job in db.due_for_ack(1011, 10)] == [second]

    assert [job["id"] for job in db.due_for_partial(1045, 45)] == [first, second]
    assert db.mark_partial_sent(first, sent_at=1045) is True
    assert db.mark_partial_sent(first, sent_at=1046) is False
    assert [job["id"] for job in db.due_for_partial(1046, 45)] == [second]


def test_evidence_is_appended_and_decoded(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)
    first = {"source": "中央气象台", "temperature": 30}
    second = {"source": "本地站", "rain": False}

    db.append_evidence(job_id, first, now=1010)
    db.append_evidence(job_id, second, now=1011)

    assert db.get(job_id)["evidence"] == [first, second]


def test_complete_requires_processing_state_and_matching_owner(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)

    assert db.complete(job_id, "cursor-a", sent_at=1010) is False
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)
    assert db.complete(job_id, "cursor-b", sent_at=1011) is False
    assert db.complete(job_id, "cursor-a", sent_at=1011) is True
    assert db.complete(job_id, "cursor-a", sent_at=1012) is False
    job = db.get(job_id)
    assert job["status"] == "done"
    assert job["final_sent_at"] == 1011


def test_fail_requires_processing_state_and_matching_owner(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)

    assert db.fail(job_id, "cursor-a", "early", available_at=1100) is False
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)
    assert db.fail(job_id, "cursor-b", "wrong", available_at=1100) is False
    assert db.fail(job_id, "cursor-a", "timeout", available_at=1100) is True
    assert db.fail(job_id, "cursor-a", "again", available_at=1200) is False
    job = db.get(job_id)
    assert job["status"] == "retry_wait"
    assert job["last_error"] == "timeout"
    assert job["available_at"] == 1100


def test_cancel_only_changes_unfinished_jobs(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    done = db.enqueue(payload(2, "u3"), detected_at=1000)
    pending = db.enqueue(payload(1), detected_at=1001)
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)
    db.complete(done, "cursor-a", sent_at=1011)

    assert db.cancel(pending, now=1012) is True
    assert db.cancel(pending, now=1013) is False
    assert db.cancel(done, now=1013) is False
    assert db.get(pending)["status"] == "cancelled"
