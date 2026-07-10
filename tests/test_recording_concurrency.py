"""Cross-process, idempotent local reply recording tests."""

import json
import multiprocessing
from pathlib import Path


def _record_worker(root_text, record, result_queue):
    from brain.recording import record_reply

    root = Path(root_text)
    try:
        record_reply(
            record,
            state_file=root / "state.json",
            inbound_dir=root / "inbound",
            history_dir=root / "history",
            lock_file=root / "record.lock",
            lock_timeout=10,
        )
    except Exception as error:
        result_queue.put(type(error).__name__)
    else:
        result_queue.put("ok")


def _write_inbound(root, conv_id, mid):
    inbound = root / "inbound"
    inbound.mkdir(exist_ok=True)
    record = {"conv_id": conv_id, "mid": mid, "direction": "in"}
    (inbound / "{}.jsonl".format(conv_id)).write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def _reply(conv_id, mid):
    return {
        "conv_id": conv_id,
        "mid": mid,
        "reply": "reply {}".format(mid),
        "markdown": False,
        "bot_uid": 7,
        "created_at": 1_000 + mid,
    }


def test_record_reply_is_idempotent_after_crash_repair(tmp_path):
    """Repeated repair cannot duplicate inbound or outbound history."""
    from brain.recording import record_reply

    _write_inbound(tmp_path, "u1", 1)
    kwargs = {
        "state_file": tmp_path / "state.json",
        "inbound_dir": tmp_path / "inbound",
        "history_dir": tmp_path / "history",
        "lock_file": tmp_path / "record.lock",
    }

    record_reply(_reply("u1", 1), **kwargs)
    record_reply(_reply("u1", 1), **kwargs)

    lines = (tmp_path / "history" / "u1.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 2
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["conversations"]["u1"]["last_processed_mid"] == 1


def test_parallel_processes_preserve_both_state_updates(tmp_path):
    """Spawned Windows-compatible writers cannot lose either cursor."""
    _write_inbound(tmp_path, "u1", 1)
    _write_inbound(tmp_path, "u2", 2)
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_record_worker,
            args=(str(tmp_path), _reply("u1", 1), results),
        ),
        context.Process(
            target=_record_worker,
            args=(str(tmp_path), _reply("u2", 2), results),
        ),
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive()
        assert process.exitcode == 0

    assert sorted(results.get(timeout=5) for _ in processes) == ["ok", "ok"]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["conversations"]["u1"]["last_processed_mid"] == 1
    assert state["conversations"]["u2"]["last_processed_mid"] == 2
