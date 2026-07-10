"""SQLite 持久任务队列的行为测试。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDataError, QueueDB


def make_payload(mid=1, conv_id="conversation-1"):
    """构造最小任务载荷。"""
    return {"conv_id": conv_id, "mid": mid, "content": "hello"}


def corrupt_field(path, job_id, field, value):
    """直接写入损坏字段以验证持久层隔离。"""
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "UPDATE jobs SET {} = ? WHERE id = ?".format(field),
            (value, job_id),
        )


def finish_job(db, job_id, owner, now):
    """Settle a job through the matching staged final transaction."""
    job = db.get(job_id)
    record = {
        "conv_id": job["conv_id"], "mid": job["mid"], "reply": "done",
        "markdown": False, "bot_uid": 7, "created_at": now,
    }
    consumer = ConsumerQueue(db)
    assert consumer.prepare_final(job_id, owner, record, now - 1, 10)
    return consumer.complete_final_pending(job_id, owner, now)


def test_package_exports_queue_db():
    """包根应公开 QueueDB。"""
    from scheduler import QueueDataError as exported_data_error
    from scheduler import QueueDB as exported_queue_db

    assert exported_queue_db is QueueDB
    assert exported_data_error is QueueDataError


def test_schema_uses_wal_and_required_index(tmp_path):
    path = tmp_path / "queue.db"
    QueueDB(path)

    with sqlite3.connect(str(path)) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        indexes = connection.execute("PRAGMA index_list(jobs)").fetchall()

    assert journal_mode.lower() == "wal"
    assert any(row[1] == "idx_jobs_available" for row in indexes)


def test_enqueue_is_idempotent_and_preserves_first_detection(tmp_path):
    db = QueueDB(tmp_path / "queue.db")

    first_id = db.enqueue(make_payload(), detected_at=100)
    second_id = db.enqueue(make_payload(), detected_at=200)

    assert second_id == first_id
    assert db.get(first_id)["detected_at"] == 100
    assert db.find("conversation-1", 1)["payload"] == make_payload()


def test_claim_is_strict_fifo_and_one_per_conversation(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    first = db.enqueue(make_payload(mid=20), detected_at=200)
    earlier_mid = db.enqueue(make_payload(mid=10), detected_at=300)

    claimed = db.claim("worker-a", now=400, limit=5, lease_seconds=30)

    assert [job["id"] for job in claimed] == [earlier_mid]
    assert db.get(first)["status"] == "pending"
    assert claimed[0]["attempts"] == 1
    assert db.claim("worker-b", now=401, limit=5, lease_seconds=30) == []


def test_claims_different_conversations_up_to_limit(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    expected = [
        db.enqueue(make_payload(1, "c1"), detected_at=100),
        db.enqueue(make_payload(2, "c2"), detected_at=200),
    ]
    db.enqueue(make_payload(3, "c3"), detected_at=300)

    claimed = db.claim("worker", now=400, limit=2, lease_seconds=10)

    assert [job["id"] for job in claimed] == expected
    assert {job["conv_id"] for job in claimed} == {"c1", "c2"}


def test_claim_never_exceeds_three_processing_conversations(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    for number in range(5):
        db.enqueue(make_payload(number, "c{}".format(number)), detected_at=100)

    claimed = db.claim("worker", now=200, limit=10, lease_seconds=10)

    assert len(claimed) == 3
    assert len({job["conv_id"] for job in claimed}) == 3


def test_claim_accounts_for_existing_processing_conversations(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    for number in range(5):
        db.enqueue(make_payload(number, "c{}".format(number)), detected_at=100)
    assert len(db.claim("first", 200, limit=2, lease_seconds=10)) == 2

    claimed = db.claim("second", 201, limit=10, lease_seconds=10)

    assert len(claimed) == 1


def test_claim_requires_available_at_to_be_due(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=500)

    assert db.claim("worker", now=499, limit=1, lease_seconds=10) == []
    assert [job["id"] for job in db.claim(
        "worker", now=500, limit=1, lease_seconds=10
    )] == [job_id]


def test_retry_wait_job_is_claimable_when_available(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=100)
    db.claim("first", now=100, limit=1, lease_seconds=10)
    assert db.fail(job_id, "first", "retry", available_at=500, now=101)

    assert db.claim("second", now=499, limit=1, lease_seconds=10) == []
    assert [job["id"] for job in db.claim(
        "second", now=500, limit=1, lease_seconds=10
    )] == [job_id]


def test_done_and_cancelled_jobs_are_not_claimable(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    done = db.enqueue(make_payload(1, "done"), detected_at=100)
    cancelled = db.enqueue(make_payload(2, "cancelled"), detected_at=100)
    db.claim("worker", now=100, limit=2, lease_seconds=10)
    assert finish_job(db, done, "worker", 200)
    assert db.cancel(cancelled, now=200)

    assert db.claim("other", now=300, limit=2, lease_seconds=10) == []


def test_recover_expired_keeps_lease_before_exact_boundary(tmp_path):
    db = QueueDB(tmp_path / "before-boundary.db")
    job_id = db.enqueue(make_payload(), detected_at=100)
    db.claim("worker", now=1010, limit=1, lease_seconds=10)

    assert db.get(job_id)["lease_until"] == 11_010
    assert db.recover_expired(now=11_009) == 0
    assert db.get(job_id)["status"] == "processing"


def test_recover_expired_recovers_at_exact_lease_boundary(tmp_path):
    db = QueueDB(tmp_path / "exact-boundary.db")
    job_id = db.enqueue(make_payload(), detected_at=100)
    db.claim("worker", now=1010, limit=1, lease_seconds=10)

    assert db.get(job_id)["lease_until"] == 11_010
    assert db.recover_expired(now=11_010) == 1
    job = db.get(job_id)
    assert job["status"] == "retry_wait"
    assert job["available_at"] == 11_010
    assert job["lease_owner"] is None
    assert job["lease_until"] is None


def test_reply_markers_are_idempotent(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=100)

    assert db.mark_ack_sent(job_id, sent_at=200) is True
    assert db.mark_ack_sent(job_id, sent_at=201) is False
    assert db.mark_partial_sent(job_id, sent_at=300) is True
    assert db.mark_partial_sent(job_id, sent_at=301) is False


def test_append_evidence_persists_structured_values(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=100)
    first = {"source": "api", "score": 0.9}
    second = {"source": "cache", "fresh": True}

    assert db._append_evidence_unchecked(job_id, first, now=200) is None
    assert db._append_evidence_unchecked(job_id, second, now=300) is None
    assert db.get(job_id)["evidence"] == [first, second]


def test_complete_and_fail_require_matching_lease_owner(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    done_id = db.enqueue(make_payload(1, "done"), detected_at=100)
    retry_id = db.enqueue(make_payload(2, "retry"), detected_at=100)
    db.claim("owner", now=200, limit=2, lease_seconds=30)

    assert db.complete(done_id, "wrong", sent_at=300) is False
    assert db.get(done_id)["status"] == "processing"
    assert db.complete(done_id, "owner", sent_at=300) is False
    assert finish_job(db, done_id, "owner", 300)
    assert db.complete(done_id, "owner", sent_at=301) is False

    assert db.fail(
        retry_id, "wrong", "error", available_at=500, now=300
    ) is False
    assert db.get(retry_id)["status"] == "processing"
    assert db.fail(
        retry_id, "owner", "error", available_at=500, now=300
    ) is True
    retry = db.get(retry_id)
    assert retry["status"] == "retry_wait"
    assert retry["last_error"] == "error"
    assert retry["available_at"] == 500
    assert retry["updated_at"] == 300


def test_complete_rejects_job_with_claimed_notification(tmp_path):
    """通知发送预约未结束时不得把任务推进为完成。"""
    from scheduler.outbox import Outbox

    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(conv_id="u1"), detected_at=100)
    db.claim("worker", now=100, limit=1, lease_seconds=100)
    assert Outbox(db).claim(job_id, "ack", "notifier", 200, 10)

    assert db.complete(job_id, "worker", sent_at=300) is False
    assert db.get(job_id)["status"] == "processing"


def test_cancelled_job_is_not_claimed(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=100)

    assert db.cancel(job_id, now=200) is True
    assert db.cancel(job_id, now=201) is False
    assert db.claim("worker", now=300, limit=1, lease_seconds=10) == []


def test_renew_requires_matching_owner(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=100)
    db.claim("owner", now=1000, limit=1, lease_seconds=10)

    assert db.renew(job_id, "wrong", now=2000, lease_seconds=20) is False
    assert db.renew(job_id, "owner", now=2000, lease_seconds=20) is True
    assert db.get(job_id)["lease_until"] == 22_000


def test_due_queries_honor_delay_markers_and_terminal_states(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    due = db.enqueue(make_payload(1, "due"), detected_at=100)
    done = db.enqueue(make_payload(2, "done"), detected_at=100)
    cancelled = db.enqueue(make_payload(3, "cancelled"), detected_at=100)
    db.claim("owner", now=100, limit=2, lease_seconds=10)
    assert finish_job(db, done, "owner", 150)
    assert db.cancel(cancelled, now=150)

    assert [job["id"] for job in db.due_for_ack(199, 100)] == []
    assert [job["id"] for job in db.due_for_ack(200, 100)] == [due]
    assert db.mark_ack_sent(due, sent_at=200)
    assert db.due_for_ack(201, 100) == []
    assert [job["id"] for job in db.due_for_partial(200, 100)] == [due]


def test_due_for_ack_excludes_jobs_with_partial_marker(tmp_path):
    """已发送阶段通知的任务不得再进入确认通知候选。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=100)
    assert db.mark_partial_sent(job_id, sent_at=200)

    assert db.due_for_ack(now=300, delay_ms=100) == []


def test_owner_operations_reject_exactly_expired_lease(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=100)
    db.claim("owner", now=1000, limit=1, lease_seconds=10)

    assert db.renew(job_id, "owner", now=11_000, lease_seconds=10) is False
    assert db.complete(job_id, "owner", sent_at=11_000) is False
    assert db.fail(
        job_id, "owner", "late", available_at=12_000, now=11_000
    ) is False
    assert db.get(job_id)["status"] == "processing"


@pytest.mark.parametrize(
    "field,value",
    (("payload_json", "{"), ("evidence_json", "{"), ("evidence_json", "{}")),
)
def test_get_and_find_raise_contextual_data_error(
    tmp_path, field, value
):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue(make_payload(), detected_at=100)
    corrupt_field(path, job_id, field, value)

    with pytest.raises(QueueDataError) as get_error:
        db.get(job_id)
    with pytest.raises(QueueDataError) as find_error:
        db.find("conversation-1", 1)

    assert get_error.value.job_id == job_id
    assert get_error.value.field == field
    assert find_error.value.field == field


@pytest.mark.parametrize(
    "field,value",
    (("payload_json", "{"), ("evidence_json", "{"), ("evidence_json", "{}")),
)
def test_claim_cancels_corrupt_job_and_fills_capacity(
    tmp_path, field, value
):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    bad = db.enqueue(make_payload(1, "bad"), detected_at=100)
    good_ids = [
        db.enqueue(make_payload(2, "good-1"), detected_at=200),
        db.enqueue(make_payload(3, "good-2"), detected_at=300),
    ]
    corrupt_field(path, bad, field, value)

    claimed = db.claim("owner", now=400, limit=2, lease_seconds=10)

    assert [job["id"] for job in claimed] == good_ids
    with sqlite3.connect(str(path)) as connection:
        status, error = connection.execute(
            "SELECT status, last_error FROM jobs WHERE id = ?", (bad,)
        ).fetchone()
    assert status == "cancelled"
    assert field in error


@pytest.mark.parametrize("method_name", ("due_for_ack", "due_for_partial"))
def test_due_query_isolates_corrupt_jobs(tmp_path, method_name):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    good = db.enqueue(make_payload(1, "good"), detected_at=100)
    bad_payload = db.enqueue(make_payload(2, "bad-payload"), detected_at=100)
    bad_evidence = db.enqueue(make_payload(3, "bad-evidence"), detected_at=100)
    corrupt_field(path, bad_payload, "payload_json", "{")
    corrupt_field(path, bad_evidence, "evidence_json", "{}")

    due = getattr(db, method_name)(now=200, delay_ms=100)

    assert [job["id"] for job in due] == [good]
    with sqlite3.connect(str(path)) as connection:
        statuses = dict(connection.execute("SELECT id, status FROM jobs"))
    assert statuses[bad_payload] == "cancelled"
    assert statuses[bad_evidence] == "cancelled"


def test_append_evidence_rejects_non_list_without_overwriting(tmp_path):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue(make_payload(), detected_at=100)
    corrupt_field(path, job_id, "evidence_json", "{}")

    with pytest.raises(QueueDataError) as error:
        db._append_evidence_unchecked(
            job_id, {"source": "test"}, now=200
        )

    assert error.value.job_id == job_id
    assert error.value.field == "evidence_json"
    with sqlite3.connect(str(path)) as connection:
        stored = connection.execute(
            "SELECT evidence_json FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert stored == "{}"


def test_set_network_mode_requires_live_owned_lease(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(make_payload(), detected_at=100)
    db.claim("owner", now=1000, limit=1, lease_seconds=10)

    with pytest.raises(ValueError):
        db.set_network_mode(job_id, "owner", "invalid", now=2000)
    assert db.set_network_mode(
        job_id, "wrong", "fast_http", now=2000
    ) is False
    assert db.set_network_mode(
        job_id, "owner", "fast_http", now=2000
    ) is True
    assert db.set_network_mode(
        job_id, "owner", "browser", now=11_000
    ) is False
    assert db.get(job_id)["network_mode"] == "fast_http"


def test_concurrent_claims_are_unique_and_globally_bounded(tmp_path):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    for number in range(6):
        db.enqueue(make_payload(number, "c{}".format(number)), detected_at=100)
    barrier = Barrier(2)

    def claim(owner):
        worker = QueueDB(path)
        barrier.wait(timeout=5)
        return worker.claim(owner, now=200, limit=3, lease_seconds=10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("owner-1", "owner-2")))

    ids = [job["id"] for result in results for job in result]
    assert len(ids) == len(set(ids)) == 3
    with sqlite3.connect(str(path)) as connection:
        count = connection.execute(
            "SELECT COUNT(DISTINCT conv_id) FROM jobs WHERE status='processing'"
        ).fetchone()[0]
    assert count <= 3


def test_claim_rolls_back_prior_update_when_later_cancel_fails(tmp_path):
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    good = db.enqueue(make_payload(1, "good"), detected_at=100)
    bad = db.enqueue(make_payload(2, "bad"), detected_at=200)
    corrupt_field(path, bad, "payload_json", "{")
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_cancel BEFORE UPDATE OF status ON jobs
            WHEN NEW.status = 'cancelled'
            BEGIN SELECT RAISE(ABORT, 'reject cancellation'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        db.claim("owner", now=300, limit=2, lease_seconds=10)

    with sqlite3.connect(str(path)) as connection:
        statuses = dict(connection.execute("SELECT id, status FROM jobs"))
    assert statuses[good] == "pending"
    assert statuses[bad] == "pending"
