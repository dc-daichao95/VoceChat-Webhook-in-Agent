"""Task 6 final delivery, evidence, migration, and reconciliation tests."""

import hashlib
import json
import sqlite3

import pytest

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.outbox import Outbox


def make_job(db, mid=1, conv_id="u1"):
    """Create one queue job with a minimal inbound payload."""
    return db.enqueue(
        {"conv_id": conv_id, "mid": mid, "content": "hello"},
        detected_at=100,
    )


def claim_job(db, owner="worker", mid=1, conv_id="u1"):
    """Create and claim one job with a long live lease."""
    job_id = make_job(db, mid, conv_id)
    assert db.claim(owner, 200, 1, 100)
    return job_id


def canonical_id(evidence):
    """Return the required canonical SHA-256 evidence identifier."""
    encoded = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_queue_complete_cannot_bypass_final_claim(tmp_path):
    """Only a matching final reservation may settle a job as done."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)

    assert not db.complete(job_id, "worker", sent_at=300)
    assert db.get(job_id)["status"] == "processing"


def test_owned_evidence_is_canonical_and_idempotent(tmp_path):
    """A retry after persistence must not duplicate the same evidence."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    consumer = ConsumerQueue(db)
    evidence = {"summary": "30°C", "source": "weather", "data": {"b": 2, "a": 1}}

    assert consumer.append_evidence_owned(job_id, evidence, "worker", 300)
    assert consumer.append_evidence_owned(job_id, evidence, "worker", 301)

    stored = db.get(job_id)["evidence"]
    assert len(stored) == 1
    assert stored[0]["evidence_id"] == canonical_id(evidence)


def test_legacy_evidence_upgrades_without_duplication(tmp_path):
    """Existing evidence_json remains readable and seeds unique IDs."""
    path = tmp_path / "legacy.db"
    db = QueueDB(path)
    job_id = claim_job(db)
    legacy = {"source": "legacy", "summary": "saved"}
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "UPDATE jobs SET evidence_json=? WHERE id=?",
            (json.dumps([legacy]), job_id),
        )
        connection.execute("PRAGMA user_version=0")

    upgraded = QueueDB(path)
    consumer = ConsumerQueue(upgraded)
    assert consumer.append_evidence_owned(
        job_id, legacy, "worker", 300
    )
    assert upgraded.get(job_id)["evidence"] == [legacy]


def test_schema_version_and_support_tables_are_migrated(tmp_path):
    """Queue initialization owns all schema objects under one version."""
    path = tmp_path / "queue.db"
    QueueDB(path)

    with sqlite3.connect(str(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert version >= 1
    assert {"deliveries", "evidence_keys", "final_records", "manual_actions"} <= tables
    assert not hasattr(__import__("scheduler.outbox", fromlist=["DELIVERY_SCHEMA"]), "DELIVERY_SCHEMA")


def test_current_schema_initialization_is_noop_without_evidence_scan(
    tmp_path, monkeypatch
):
    """Opening a current database does not run version-zero data migration."""
    from scheduler import schema

    path = tmp_path / "queue.db"
    QueueDB(path)

    def forbidden(connection):
        raise AssertionError("current schema must not scan jobs")

    monkeypatch.setattr(schema, "_seed_evidence_keys", forbidden)
    QueueDB(path)


def test_version_one_upgrades_to_current_version(tmp_path):
    """Databases created by the prior Task6 schema take the 1->2 step."""
    from scheduler.schema import CURRENT_SCHEMA_VERSION

    path = tmp_path / "queue.db"
    QueueDB(path)
    with sqlite3.connect(str(path)) as connection:
        connection.execute("PRAGMA user_version=1")

    QueueDB(path)

    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            CURRENT_SCHEMA_VERSION
        )


def test_future_schema_version_is_rejected_without_rewrite(tmp_path):
    """Unknown future databases fail closed and preserve their version."""
    from scheduler.schema import CURRENT_SCHEMA_VERSION

    path = tmp_path / "queue.db"
    QueueDB(path)
    future = CURRENT_SCHEMA_VERSION + 1
    with sqlite3.connect(str(path)) as connection:
        connection.execute("PRAGMA user_version={}".format(future))

    with pytest.raises(RuntimeError):
        QueueDB(path)

    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == future


def test_list_exposes_final_block_and_record_state(tmp_path):
    """Operational listing shows final state without exposing reply content."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, "final", "worker", 300, 1)
    assert not outbox.claim(job_id, "final", "other", 1_300, 1)

    listed = ConsumerQueue(db).list_jobs()

    assert listed[0]["final_delivery_state"] == "uncertain"
    assert listed[0]["block_reason"] == "LeaseExpired"
    assert listed[0]["record_pending"] is False


class Response:
    """Minimal send response used by final delivery tests."""

    def __init__(self, ok=True, status_code=200):
        self.ok = ok
        self.status_code = status_code


class FakeClock:
    """Return deterministic millisecond timestamps in order."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


def test_recover_expired_atomically_blocks_prepared_final(tmp_path):
    """A crashed prepared final becomes uncertain before job recovery."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    consumer = ConsumerQueue(db)
    record = {
        "conv_id": "u1", "mid": 1, "reply": "private",
        "markdown": False, "bot_uid": 7, "created_at": 300,
    }
    assert consumer.prepare_final(job_id, "worker", record, 300, 200)

    assert db.recover_expired(100_200) == 1

    delivery = Outbox(db).state(job_id, "final")
    assert delivery["state"] == "uncertain"
    assert delivery["owner"] is None
    assert delivery["lease_until"] is None
    assert delivery["last_error"] == "LeaseExpired"
    assert delivery["attempted_at"] == 300
    assert db.get(job_id)["status"] == "retry_wait"
    assert db.claim("next", 100_200, 1, 10) == []
    assert consumer.reconcile(job_id, "mark-done", 100_300) == "done"


def test_send_success_record_failure_repairs_without_resend(tmp_path):
    """A local record failure after 2xx remains done and repairable."""
    from scheduler.final_delivery import repair_record, send_final

    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    calls = []

    def sender(*args, **kwargs):
        with sqlite3.connect(str(db.path)) as connection:
            state, encoded = connection.execute(
                "SELECT record_state,reply_json FROM final_records "
                "WHERE job_id=?", (job_id,)
            ).fetchone()
        assert state == "prepared"
        assert json.loads(encoded)["reply"] == "private reply"
        calls.append((args, kwargs))
        return Response()

    def broken_record(payload):
        raise OSError("disk unavailable")

    result = send_final(
        db, job_id, "worker", "private reply", False,
        "https://chat.example", "secret-key", 300,
        sender=sender, recorder=broken_record,
    )

    assert result == {
        "job_id": job_id, "status": "done", "record_pending": True
    }
    assert db.get(job_id)["status"] == "done"
    assert Outbox(db).state(job_id, "final")["state"] == "sent"
    assert ConsumerQueue(db).list_jobs()[0]["record_pending"] is True
    recorded = []
    assert repair_record(db, job_id, 400, recorder=recorded.append)
    assert recorded[0]["reply"] == "private reply"
    assert ConsumerQueue(db).list_jobs()[0]["record_pending"] is False
    assert len(calls) == 1


def test_unknown_send_result_is_uncertain_and_never_resent(tmp_path):
    """A transport exception freezes final delivery at at-most-once."""
    from scheduler.final_delivery import send_final

    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    calls = []

    def sender(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("unknown result")

    result = send_final(
        db, job_id, "worker", "reply", False,
        "https://chat.example", "key", 300, sender=sender,
    )
    blocked = send_final(
        db, job_id, "worker", "reply", False,
        "https://chat.example", "key", 400, sender=sender,
    )

    assert result["status"] == "uncertain"
    assert blocked["status"] == "blocked"
    assert Outbox(db).state(job_id, "final")["state"] == "uncertain"
    assert len(calls) == 1


def test_allowlisted_client_rejection_can_retry(tmp_path):
    """A conservative 4xx known not to process can release for retry."""
    from scheduler.final_delivery import send_final

    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    result = send_final(
        db, job_id, "worker", "reply", False,
        "https://chat.example", "key", 300,
        sender=lambda *args, **kwargs: Response(True, 400),
    )

    assert result["status"] == "failed"
    assert db.get(job_id)["status"] == "retry_wait"
    assert Outbox(db).state(job_id, "final")["state"] == "failed"


@pytest.mark.parametrize("status", (302, 429, 500, 503))
def test_ambiguous_http_status_never_opens_automatic_retry(
    tmp_path, status
):
    """Redirects, throttling, and server errors may follow a committed send."""
    from scheduler.final_delivery import send_final

    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    result = send_final(
        db, job_id, "worker", "reply", False,
        "https://chat.example", "key", 300,
        sender=lambda *args, **kwargs: Response(False, status),
    )

    assert result["status"] == "uncertain"
    assert Outbox(db).state(job_id, "final")["state"] == "uncertain"
    assert db.claim("next", 60_300, 1, 10) == []


@pytest.mark.parametrize("delivery_state", ("uncertain", "sent"))
def test_direct_cancel_rejects_final_uncertain_or_sent(
    tmp_path, delivery_state
):
    """QueueDB and ConsumerQueue cannot bypass final reconciliation."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    consumer = ConsumerQueue(db)
    record = {
        "conv_id": "u1", "mid": 1, "reply": "private",
        "markdown": False, "bot_uid": 7, "created_at": 300,
    }
    assert consumer.prepare_final(job_id, "worker", record, 300, 100)
    if delivery_state == "uncertain":
        assert Outbox(db).mark_final_uncertain(
            job_id, "worker", 400, "TransportError"
        )
    else:
        assert consumer.complete_final_pending(job_id, "worker", 400)

    assert not db.cancel(job_id, 500)
    assert not consumer.cancel(job_id, "worker", 500)


@pytest.mark.parametrize("kind", ("ack", "partial"))
def test_db_cancel_allows_sent_nonfinal_delivery(tmp_path, kind):
    """Sent SLA notifications do not prevent ordinary queue cancellation."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = make_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, kind, "notifier", 200, 10)
    assert outbox.mark_sent(job_id, kind, "notifier", 300)

    assert db.cancel(job_id, 400)


@pytest.mark.parametrize("kind", ("ack", "partial"))
def test_consumer_cancel_allows_sent_nonfinal_delivery(tmp_path, kind):
    """A live owner may cancel after a completed SLA notification."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    outbox = Outbox(db)
    assert outbox.claim(job_id, kind, "notifier", 300, 10)
    assert outbox.mark_sent(job_id, kind, "notifier", 400)

    assert ConsumerQueue(db).cancel(job_id, "worker", 500)


@pytest.mark.parametrize("kind", ("ack", "partial"))
def test_claimed_nonfinal_delivery_blocks_both_cancel_paths(tmp_path, kind):
    """Any in-flight delivery blocks cancellation until send settles."""
    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    assert Outbox(db).claim(job_id, kind, "notifier", 300, 10)

    assert not db.cancel(job_id, 400)
    assert not ConsumerQueue(db).cancel(job_id, "worker", 400)


def test_record_pending_blocks_only_same_conversation(tmp_path):
    """Later same-conversation work waits until local record repair."""
    from scheduler.final_delivery import repair_record

    db = QueueDB(tmp_path / "queue.db")
    first = make_job(db, 1, "u1")
    second = make_job(db, 2, "u1")
    other = make_job(db, 1, "u2")
    assert db.claim("worker", 200, 1, 100)[0]["id"] == first
    consumer = ConsumerQueue(db)
    record = {
        "conv_id": "u1", "mid": 1, "reply": "private",
        "markdown": False, "bot_uid": 7, "created_at": 300,
    }
    assert consumer.prepare_final(first, "worker", record, 300, 100)
    assert consumer.complete_final_pending(first, "worker", 400)

    claimed = db.claim("next", 500, 3, 100)
    assert [job["id"] for job in claimed] == [other]
    assert repair_record(db, first, 600, recorder=lambda value: None)
    assert db.claim("next", 700, 1, 100)[0]["id"] == second


def test_2xx_after_send_lease_expiry_becomes_uncertain(tmp_path):
    """A late successful response cannot complete with an expired lease."""
    from scheduler.final_delivery import send_final

    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    result = send_final(
        db, job_id, "worker", "reply", False,
        "https://chat.example", "key",
        clock=FakeClock(300, 120_301),
        sender=lambda *args, **kwargs: Response(True, 200),
    )

    assert result["status"] == "uncertain"
    assert db.get(job_id)["status"] == "retry_wait"
    assert Outbox(db).state(job_id, "final")["state"] == "uncertain"
    assert db.claim("next", 120_301, 1, 10) == []


def test_redirect_is_uncertain_even_when_ok_true(tmp_path):
    """A 3xx may follow server-side processing and cannot auto-retry."""
    from scheduler.final_delivery import send_final

    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    result = send_final(
        db, job_id, "worker", "reply", False,
        "https://chat.example", "key",
        clock=FakeClock(300, 400),
        sender=lambda *args, **kwargs: Response(True, 302),
    )

    assert result["status"] == "uncertain"
    assert Outbox(db).state(job_id, "final")["state"] == "uncertain"


def test_missing_status_is_conservatively_uncertain(tmp_path):
    """A response without a valid integer status can never be retried."""
    from scheduler.final_delivery import send_final

    db = QueueDB(tmp_path / "queue.db")
    job_id = claim_job(db)
    response = object()
    result = send_final(
        db, job_id, "worker", "reply", False,
        "https://chat.example", "key",
        clock=FakeClock(300, 400),
        sender=lambda *args, **kwargs: response,
    )

    assert result["status"] == "uncertain"
    assert Outbox(db).state(job_id, "final")["state"] == "uncertain"
