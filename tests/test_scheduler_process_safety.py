"""Independent-process schema and final transition tests."""

import multiprocessing
import sqlite3

from scheduler.db import QueueDB


def _initialize_worker(path, results):
    try:
        QueueDB(path)
    except Exception as error:
        results.put(type(error).__name__)
    else:
        results.put("ok")


def _prepare_worker(path, job_id, results):
    from scheduler.consumer import ConsumerQueue

    record = {
        "conv_id": "u1", "mid": 1, "reply": "private",
        "markdown": False, "bot_uid": 7, "created_at": 300,
    }
    result = ConsumerQueue(QueueDB(path)).prepare_final(
        job_id, "owner", record, 300, 120
    )
    results.put(result)


def _complete_worker(path, job_id, results):
    from scheduler.consumer import ConsumerQueue

    result = ConsumerQueue(QueueDB(path)).complete_final_pending(
        job_id, "owner", 400
    )
    results.put(result)


def _recover_worker(path, now, start, results):
    start.wait(timeout=10)
    results.put(("recover", QueueDB(path).recover_expired(now)))


def _claim_worker(path, now, start, results):
    start.wait(timeout=10)
    claimed = QueueDB(path).claim("next", now, 1, 10)
    results.put(("claim", bool(claimed)))


def _run_process(context, target, args):
    results = context.Queue()
    process = context.Process(target=target, args=args + (results,))
    process.start()
    process.join(timeout=20)
    assert not process.is_alive()
    assert process.exitcode == 0
    return results.get(timeout=5)


def test_final_prepare_and_complete_work_across_processes(tmp_path):
    """Separate Windows-spawned processes share the final transaction."""
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue({"conv_id": "u1", "mid": 1}, 100)
    assert db.claim("owner", 200, 1, 120)
    context = multiprocessing.get_context("spawn")

    assert _run_process(
        context, _prepare_worker, (str(path), job_id)
    )
    assert _run_process(
        context, _complete_worker, (str(path), job_id)
    )

    assert db.get(job_id)["status"] == "done"
    with sqlite3.connect(str(path)) as connection:
        state = connection.execute(
            "SELECT state FROM deliveries WHERE job_id=? AND kind='final'",
            (job_id,),
        ).fetchone()[0]
    assert state == "sent"


def test_concurrent_initialization_upgrades_old_database(tmp_path):
    """Migration lock serializes real concurrent initialization."""
    path = tmp_path / "old.db"
    QueueDB(path)
    with sqlite3.connect(str(path)) as connection:
        connection.execute("DROP TABLE evidence_keys")
        connection.execute("DROP TABLE final_records")
        connection.execute("DROP TABLE manual_actions")
        connection.execute("PRAGMA user_version=0")
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_initialize_worker, args=(str(path), results)
        )
        for _ in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive()
        assert process.exitcode == 0

    assert [results.get(timeout=5) for _ in processes] == ["ok"] * 4
    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 1


def test_recover_and_claim_are_atomic_for_prepared_final(tmp_path):
    """Concurrent recovery never exposes a prepared final for reclaim."""
    from scheduler.consumer import ConsumerQueue
    from scheduler.outbox import Outbox

    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue({"conv_id": "u1", "mid": 1}, 100)
    assert db.claim("owner", 200, 1, 1)
    record = {
        "conv_id": "u1", "mid": 1, "reply": "private",
        "markdown": False, "bot_uid": 7, "created_at": 300,
    }
    assert ConsumerQueue(db).prepare_final(
        job_id, "owner", record, 300, 100
    )
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    processes = [
        context.Process(
            target=_recover_worker,
            args=(str(path), 1_200, start, results),
        ),
        context.Process(
            target=_claim_worker,
            args=(str(path), 1_200, start, results),
        ),
    ]

    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive() and process.exitcode == 0

    outcomes = dict(results.get(timeout=5) for _ in processes)
    assert outcomes == {"recover": 1, "claim": False}
    assert Outbox(db).state(job_id, "final")["state"] == "uncertain"


def test_concurrent_claims_wait_for_prior_record_repair(tmp_path):
    """Two processes cannot pass a same-conversation pending record."""
    from scheduler.consumer import ConsumerQueue
    from scheduler.final_delivery import repair_record

    path = tmp_path / "queue.db"
    db = QueueDB(path)
    first = db.enqueue({"conv_id": "u1", "mid": 1}, 100)
    second = db.enqueue({"conv_id": "u1", "mid": 2}, 100)
    assert db.claim("owner", 200, 1, 100)[0]["id"] == first
    record = {
        "conv_id": "u1", "mid": 1, "reply": "private",
        "markdown": False, "bot_uid": 7, "created_at": 300,
    }
    consumer = ConsumerQueue(db)
    assert consumer.prepare_final(first, "owner", record, 300, 100)
    assert consumer.complete_final_pending(first, "owner", 400)
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    processes = [
        context.Process(
            target=_claim_worker,
            args=(str(path), 500, start, results),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive() and process.exitcode == 0

    assert [results.get(timeout=5)[1] for _ in processes] == [False, False]
    assert repair_record(db, first, 600, recorder=lambda value: None)
    assert db.claim("next", 700, 1, 10)[0]["id"] == second
