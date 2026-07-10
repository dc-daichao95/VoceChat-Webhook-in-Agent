"""Reliable final-send and operational queue CLI tests."""

import json
import sqlite3

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.outbox import Outbox
from scripts import queue_cli


def _invoke(capsys, argv):
    code = queue_cli.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def _uncertain_job(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue({"conv_id": "u1", "mid": 1}, 100)
    assert db.claim("owner", 200, 1, 100)
    record = {
        "conv_id": "u1", "mid": 1, "reply": "private",
        "markdown": False, "bot_uid": 7, "created_at": 300,
    }
    consumer = ConsumerQueue(db)
    assert consumer.prepare_final(job_id, "owner", record, 300, 1)
    assert Outbox(db).fail_final(
        job_id, "owner", 400, "Timeout", 500, uncertain=True
    )
    return path, db, job_id


def test_send_final_cli_never_echoes_reply(tmp_path, monkeypatch, capsys):
    """send-final delegates staged sending without exposing reply text."""
    path = tmp_path / "queue.db"
    QueueDB(path)
    reply_file = tmp_path / "reply-secret.txt"
    reply_file.write_text("private reply body", encoding="utf-8")

    def fake_send(*args, **kwargs):
        assert args[3] == "private reply body"
        return {"job_id": 1, "status": "done", "record_pending": False}

    monkeypatch.setattr(queue_cli, "send_final", fake_send)
    monkeypatch.setenv("VOCECHAT_SERVER_URL", "https://private.example")
    monkeypatch.setenv("VOCECHAT_API_KEY", "secret-key")

    code, result = _invoke(
        capsys,
        [
            "--db", str(path), "send-final", "--job-id", "1",
            "--owner", "owner", "--reply-file", str(reply_file),
        ],
    )

    assert code == 0
    assert result["status"] == "done"
    assert "private reply" not in json.dumps(result)


def test_evidence_file_error_is_input_error(tmp_path, capsys):
    """Unreadable evidence is not mislabeled as a database failure."""
    path = tmp_path / "queue.db"
    QueueDB(path)

    code, result = _invoke(
        capsys,
        [
            "--db", str(path), "evidence", "--job-id", "1",
            "--owner", "owner", "--file", str(tmp_path / "missing.json"),
        ],
    )

    assert code == 3
    assert result["error"] == "input_error"


def test_unknown_cli_exception_is_internal_error(monkeypatch, capsys):
    """Unknown exceptions map to one fixed safe category."""
    monkeypatch.setattr(
        queue_cli, "QueueDB",
        lambda path: (_ for _ in ()).throw(RuntimeError("private secret")),
    )

    code, result = _invoke(
        capsys, ["list"]
    )

    assert code == 1
    assert result == {
        "command": "list", "error": "internal_error", "ok": False
    }


def test_reconcile_requires_confirmation_and_audits_retry(
    tmp_path, capsys
):
    """Uncertain retry requires explicit duplicate-risk confirmation."""
    path, db, job_id = _uncertain_job(tmp_path)
    common = [
        "--db", str(path), "reconcile", "--job-id", str(job_id),
        "--action", "retry", "--confirm",
    ]

    denied, denied_result = _invoke(capsys, common)
    allowed, allowed_result = _invoke(
        capsys, common + ["--confirm-duplicate-risk"]
    )

    assert denied == 3
    assert denied_result["error"] == "confirmation_required"
    assert allowed == 0
    assert allowed_result["status"] == "retry_wait"
    assert db.get(job_id)["status"] == "retry_wait"
    with sqlite3.connect(str(path)) as connection:
        action = connection.execute(
            "SELECT action,risk_confirmed FROM manual_actions"
        ).fetchone()
    assert action == ("retry", 1)


def test_reconcile_can_mark_done_or_cancel_without_live_lease(
    tmp_path, capsys
):
    """Confirmed operations resolve uncertain jobs without an owner lease."""
    done_path, done_db, done_id = _uncertain_job(tmp_path / "done")
    cancel_path, cancel_db, cancel_id = _uncertain_job(tmp_path / "cancel")

    done_code, _ = _invoke(
        capsys,
        [
            "--db", str(done_path), "reconcile", "--job-id", str(done_id),
            "--action", "mark-done", "--confirm",
        ],
    )
    cancel_code, _ = _invoke(
        capsys,
        [
            "--db", str(cancel_path), "reconcile",
            "--job-id", str(cancel_id), "--action", "cancel", "--confirm",
        ],
    )

    assert done_code == cancel_code == 0
    done_job = done_db.get(done_id)
    assert done_job["status"] == "done"
    assert done_job["lease_owner"] is None
    assert done_job["lease_until"] is None
    assert ConsumerQueue(done_db).list_jobs()[0]["record_pending"] is True
    assert cancel_db.get(cancel_id)["status"] == "cancelled"


def test_list_exposes_block_state_without_reply_body(tmp_path, capsys):
    """Operational JSON includes final state and never includes reply text."""
    path, _, _ = _uncertain_job(tmp_path)

    code, result = _invoke(capsys, ["--db", str(path), "list"])

    assert code == 0
    assert result["final_delivery_state"] == "uncertain"
    assert result["block_reason"] == "Timeout"
    assert result["record_pending"] is False
    assert "private" not in json.dumps(result)
