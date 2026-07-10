"""Behavior tests for the durable SQLite scheduler queue."""

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scheduler.db import QueueDataError, QueueDB


def payload(mid=1431, conv_id="u2"):
    """Build a minimal inbound message payload."""
    return {
        "mid": mid,
        "conv_id": conv_id,
        "direction": "in",
        "content": "查天气",
    }


def corrupt_field(path, job_id, field, value):
    """Replace one encoded queue field with invalid persisted data."""
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "UPDATE jobs SET {} = ? WHERE id = ?".format(field),
            (value, job_id),
        )


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


def test_claim_limit_includes_existing_processing_conversations(tmp_path):
    path = tmp_path / "queue.db"
    first = QueueDB(path)
    for number in range(1, 6):
        first.enqueue(payload(number, "u{}".format(number)), 1000 + number)
    assert len(first.claim("cursor-a", 1010, limit=2, lease_seconds=60)) == 2

    second = QueueDB(path)
    claimed = second.claim("cursor-b", 1011, limit=3, lease_seconds=60)
    blocked = first.claim("cursor-c", 1012, limit=3, lease_seconds=60)

    assert len(claimed) == 1
    assert blocked == []


def test_concurrent_claimers_cannot_exceed_global_limit(tmp_path):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    for number in range(1, 7):
        db.enqueue(payload(number, "u{}".format(number)), 1000 + number)
    barrier = Barrier(2)

    def claim(owner):
        worker_db = QueueDB(path)
        barrier.wait()
        return worker_db.claim(owner, 1010, limit=3, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("cursor-a", "cursor-b")))

    assert sum(len(result) for result in results) == 3
    assert len({job["conv_id"] for result in results for job in result}) == 3


def test_claim_does_not_skip_unavailable_earlier_job(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    first = db.enqueue(payload(1), detected_at=1000)
    second = db.enqueue(payload(2), detected_at=1001)
    claimed = db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)
    assert [job["id"] for job in claimed] == [first]
    assert db.fail(first, "cursor-a", "wait", available_at=1100, now=1011)

    assert db.claim("cursor-b", now=1050, limit=1, lease_seconds=10) == []
    assert db.get(second)["status"] == "pending"


def test_renew_requires_processing_job_and_matching_owner(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)

    assert db.renew(job_id, "cursor-b", now=1011, lease_seconds=20) is False
    assert db.renew(job_id, "cursor-a", now=1011, lease_seconds=20) is True
    assert db.get(job_id)["lease_until"] == 1031


def test_owner_operations_reject_expired_lease(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)

    assert db.renew(job_id, "cursor-a", now=1020, lease_seconds=20) is False
    assert db.complete(job_id, "cursor-a", sent_at=1020) is False
    assert db.fail(
        job_id, "cursor-a", "late", available_at=1100, now=1020
    ) is False


def test_old_owner_cannot_mutate_job_after_recovery(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)
    assert db.recover_expired(now=1021) == 1

    assert db.renew(job_id, "cursor-a", now=1021, lease_seconds=20) is False
    assert db.complete(job_id, "cursor-a", sent_at=1021) is False
    assert db.fail(
        job_id, "cursor-a", "late", available_at=1100, now=1021
    ) is False


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


@pytest.mark.parametrize(
    "field,value",
    (("payload_json", "{"), ("evidence_json", "{}")),
)
def test_get_and_find_raise_clear_data_error(tmp_path, field, value):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue(payload(), detected_at=1000)
    corrupt_field(path, job_id, field, value)

    with pytest.raises(QueueDataError) as get_error:
        db.get(job_id)
    with pytest.raises(QueueDataError) as find_error:
        db.find("u2", 1431)

    assert get_error.value.job_id == job_id
    assert get_error.value.field == field
    assert find_error.value.field == field


@pytest.mark.parametrize("method_name", ("due_for_ack", "due_for_partial"))
def test_due_queries_skip_and_cancel_corrupt_jobs(tmp_path, method_name):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    good = db.enqueue(payload(1, "u1"), detected_at=1000)
    bad_payload = db.enqueue(payload(2, "u2"), detected_at=1000)
    bad_evidence = db.enqueue(payload(3, "u3"), detected_at=1000)
    corrupt_field(path, bad_payload, "payload_json", "{")
    corrupt_field(path, bad_evidence, "evidence_json", "{}")

    due = getattr(db, method_name)(now=1100, delay_ms=10)

    assert [job["id"] for job in due] == [good]
    with sqlite3.connect(str(path)) as connection:
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT id, status, last_error FROM jobs"
            )
        }
    assert rows[bad_payload][0] == "cancelled"
    assert "corrupt payload_json" in rows[bad_payload][1]
    assert rows[bad_evidence][0] == "cancelled"
    assert "corrupt evidence_json" in rows[bad_evidence][1]


@pytest.mark.parametrize("value", ("{", "{}"))
def test_append_evidence_rejects_corrupt_or_non_list_data(tmp_path, value):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue(payload(), detected_at=1000)
    corrupt_field(path, job_id, "evidence_json", value)

    with pytest.raises(QueueDataError) as error:
        db.append_evidence(job_id, {"source": "test"}, now=1010)

    assert error.value.job_id == job_id
    assert error.value.field == "evidence_json"


def test_connection_context_closes_after_exception(tmp_path):
    path = tmp_path / "queue.db"
    renamed = tmp_path / "renamed.db"
    db = QueueDB(path)

    with pytest.raises(RuntimeError):
        with db._connect() as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            raise RuntimeError("force rollback")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    os.replace(str(path), str(renamed))
    assert renamed.exists()


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

    assert db.fail(
        job_id, "cursor-a", "early", available_at=1100, now=1009
    ) is False
    db.claim("cursor-a", now=1010, limit=1, lease_seconds=10)
    assert db.fail(
        job_id, "cursor-b", "wrong", available_at=1100, now=1011
    ) is False
    assert db.fail(
        job_id, "cursor-a", "timeout", available_at=1100, now=1011
    ) is True
    assert db.fail(
        job_id, "cursor-a", "again", available_at=1200, now=1012
    ) is False
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
