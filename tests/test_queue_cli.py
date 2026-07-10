"""Cursor 队列 CLI、final outbox 与消费者手册契约测试。"""

import json
from pathlib import Path

import pytest

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.outbox import Outbox
from scripts import queue_cli


NOW = 1_000_000
OWNER = "cursor-test"
ROOT = Path(__file__).parents[1]


def payload(mid=1, conv_id="u1"):
    """构造包含不应出现在 CLI 输出中的敏感载荷。"""
    return {
        "mid": mid,
        "conv_id": conv_id,
        "content": "payload https://private.example/?api_key=secret",
    }


def invoke(capsys, argv):
    """调用 CLI 并解析 JSON Lines。"""
    code = queue_cli.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = [json.loads(line) for line in captured.out.splitlines()]
    return code, lines, captured.out


def make_processing(tmp_path, monkeypatch):
    """创建由固定 owner 持有有效租约的任务。"""
    monkeypatch.setattr(queue_cli, "_now_ms", lambda: NOW)
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue(payload(), NOW)
    assert db.claim(OWNER, NOW, 1, 120)[0]["attempts"] == 1
    return path, db, job_id


def test_next_claims_fifo_sessions_and_redacts_payload(
    tmp_path, monkeypatch, capsys
):
    """next 每会话只领一个，并输出稳定安全字段。"""
    monkeypatch.setattr(queue_cli, "_now_ms", lambda: NOW)
    path = tmp_path / "private-queue-secret.db"
    db = QueueDB(path)
    first = db.enqueue(payload(1, "u1"), NOW)
    db.enqueue(payload(2, "u1"), NOW)
    second = db.enqueue(payload(3, "g2"), NOW)

    code, lines, output = invoke(
        capsys,
        ["--db", str(path), "next", "--owner", OWNER, "--limit", "3"],
    )

    assert code == 0
    assert [item["id"] for item in lines] == [first, second]
    assert all(item["event"] == "job" for item in lines)
    assert all(item["attempts"] == 1 for item in lines)
    for secret in ("payload", "private-queue-secret", "api_key", "private.example"):
        assert secret not in output


def test_next_empty_is_stable_json(tmp_path, monkeypatch, capsys):
    """空队列也输出单个稳定事件。"""
    monkeypatch.setattr(queue_cli, "_now_ms", lambda: NOW)
    path = tmp_path / "queue.db"
    QueueDB(path)

    code, lines, _ = invoke(
        capsys, ["--db", str(path), "next", "--owner", OWNER]
    )

    assert code == 0
    assert lines == [{"event": "empty"}]


def test_fail_uses_one_based_attempt_without_increment(
    tmp_path, monkeypatch, capsys
):
    """首次领取失败直接采用 attempts=1 的 60 秒退避。"""
    path, db, job_id = make_processing(tmp_path, monkeypatch)

    code, lines, output = invoke(
        capsys,
        [
            "--db", str(path), "fail", "--job-id", str(job_id),
            "--owner", OWNER, "--error",
            "browser timeout https://private/?key=secret",
        ],
    )

    assert code == 0
    assert lines == [{"command": "fail", "job_id": job_id, "ok": True}]
    job = db.get(job_id)
    assert job["status"] == "retry_wait"
    assert job["available_at"] == NOW + 60_000
    assert job["attempts"] == 1
    assert job["last_error"] == "InternalError"
    assert "private" not in output


@pytest.mark.parametrize("command", ("renew", "fail", "cancel"))
def test_owner_commands_reject_wrong_owner(
    command, tmp_path, monkeypatch, capsys
):
    """续租、失败和取消都验证 owner、状态和租约。"""
    path, db, job_id = make_processing(tmp_path, monkeypatch)
    argv = [
        "--db", str(path), command, "--job-id", str(job_id),
        "--owner", "wrong",
    ]
    if command == "fail":
        argv += ["--error", "Timeout"]

    code, lines, _ = invoke(capsys, argv)

    assert code == 3
    assert lines == [{
        "command": command, "error": "transition_rejected", "ok": False
    }]
    assert db.get(job_id)["status"] == "processing"


@pytest.mark.parametrize("command", ("renew", "fail", "cancel"))
def test_owner_commands_reject_expired_lease(
    command, tmp_path, monkeypatch, capsys
):
    """租约边界到达后，原 owner 也不得转换任务。"""
    path, db, job_id = make_processing(tmp_path, monkeypatch)
    monkeypatch.setattr(queue_cli, "_now_ms", lambda: NOW + 120_000)
    argv = [
        "--db", str(path), command, "--job-id", str(job_id),
        "--owner", OWNER,
    ]
    if command == "fail":
        argv += ["--error", "Timeout"]

    code, _, _ = invoke(capsys, argv)

    assert code == 3
    assert db.get(job_id)["status"] == "processing"


def test_evidence_and_mode_require_live_owner(
    tmp_path, monkeypatch, capsys
):
    """证据和联网分类只允许当前任务 owner 写入。"""
    path, db, job_id = make_processing(tmp_path, monkeypatch)
    evidence_path = tmp_path / "private-evidence-secret.json"
    evidence = {
        "source": "api",
        "url": "https://private.example/?key=secret",
        "summary": "private result",
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    wrong, _, _ = invoke(
        capsys,
        [
            "--db", str(path), "evidence", "--job-id", str(job_id),
            "--owner", "wrong", "--file", str(evidence_path),
        ],
    )
    mode, _, _ = invoke(
        capsys,
        [
            "--db", str(path), "mode", "--job-id", str(job_id),
            "--owner", OWNER, "--value", "fast_http",
        ],
    )
    stored, lines, output = invoke(
        capsys,
        [
            "--db", str(path), "evidence", "--job-id", str(job_id),
            "--owner", OWNER, "--file", str(evidence_path),
        ],
    )

    assert wrong == 3
    assert mode == stored == 0
    assert lines == [{
        "command": "evidence", "job_id": job_id, "ok": True
    }]
    assert db.get(job_id)["network_mode"] == "fast_http"
    stored_evidence = db.get(job_id)["evidence"]
    assert stored_evidence[0]["source"] == evidence["source"]
    assert stored_evidence[0]["summary"] == evidence["summary"]
    assert len(stored_evidence[0]["evidence_id"]) == 64
    assert "private" not in output


def test_append_evidence_owned_rejects_stale_missing_expired_and_done(
    tmp_path
):
    """原子证据写入只接受当前 processing 租约 owner。"""
    db = QueueDB(tmp_path / "queue.db")
    consumer = ConsumerQueue(db)
    current = db.enqueue(payload(1, "u1"), 100)
    expired = db.enqueue(payload(2, "u2"), 100)
    done = db.enqueue(payload(3, "u3"), 100)
    assert db.claim("current", 200, 3, 10)
    evidence = {"source": "api", "summary": "ok"}

    assert not consumer.append_evidence_owned(
        current, evidence, "stale", 300
    )
    assert not consumer.append_evidence_owned(
        current, evidence, None, 300
    )
    assert consumer.append_evidence_owned(
        current, evidence, "current", 300
    )
    assert db.fail(
        current, "current", "retry", available_at=400, now=350
    )
    assert db.claim("replacement", 400, 1, 10)[0]["id"] == current
    assert not consumer.append_evidence_owned(
        current, evidence, "current", 500
    )
    replacement = {"source": "replacement", "summary": "new"}
    assert consumer.append_evidence_owned(
        current, replacement, "replacement", 500
    )
    assert not consumer.append_evidence_owned(
        expired, evidence, "current", 10_200
    )
    outbox = Outbox(db)
    assert outbox.claim(done, "final", "current", 250, 5)
    assert outbox.mark_sent(done, "final", "current", 300)
    assert not consumer.append_evidence_owned(
        done, evidence, "current", 400
    )
    stored = db.get(current)["evidence"]
    assert [item["source"] for item in stored] == ["api", "replacement"]
    assert all(len(item["evidence_id"]) == 64 for item in stored)
    assert db.get(expired)["evidence"] == []
    assert db.get(done)["evidence"] == []


def test_evidence_cli_requires_explicit_owner(
    tmp_path, monkeypatch, capsys
):
    """evidence 不得从数据库猜测 owner。"""
    path, db, job_id = make_processing(tmp_path, monkeypatch)
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text('{"source":"api"}', encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        queue_cli.main(
            [
                "--db", str(path), "evidence", "--job-id", str(job_id),
                "--file", str(evidence_path),
            ]
        )

    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"
    assert db.get(job_id)["evidence"] == []


@pytest.mark.parametrize("command", ("final", "final-claim", "complete"))
def test_unsafe_final_commands_are_not_public(command, capsys):
    """Public CLI exposes only staged send-final for formal replies."""
    with pytest.raises(SystemExit) as error:
        queue_cli.main(
            [command, "--job-id", "1", "--owner", OWNER]
        )

    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


def test_list_and_errors_do_not_leak_secrets(
    tmp_path, monkeypatch, capsys
):
    """列表与异常只输出安全摘要和稳定错误。"""
    monkeypatch.setattr(queue_cli, "_now_ms", lambda: NOW)
    path = tmp_path / "private-list-secret.db"
    db = QueueDB(path)
    job_id = db.enqueue(payload(), NOW)
    code, lines, output = invoke(
        capsys, ["--db", str(path), "list", "--status", "pending"]
    )
    assert code == 0
    assert lines[0]["id"] == job_id
    assert set(lines[0]).isdisjoint(
        {"payload", "evidence", "last_error", "lease_owner"}
    )
    assert "private" not in output

    invalid = tmp_path / "private-db-api-key-secret"
    invalid.mkdir()
    code, lines, output = invoke(
        capsys, ["--db", str(invalid), "next", "--owner", OWNER]
    )
    assert code == 1
    assert lines == [{
        "command": "next", "error": "database_error", "ok": False
    }]
    assert "secret" not in output


def test_usage_error_is_redacted_json(capsys):
    """参数错误不得回显原始参数。"""
    with pytest.raises(SystemExit) as error:
        queue_cli.main(
            [
                "--db", "C:\\private\\api-key-secret.db", "fail",
                "--error", "https://private/?key=secret",
            ]
        )

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "command": "usage", "error": "invalid_arguments", "ok": False
    }
    assert "secret" not in captured.out


def test_blank_owner_is_rejected_before_database_access(capsys):
    """空白 owner 不得成为可领取或转换任务的有效身份。"""
    with pytest.raises(SystemExit) as error:
        queue_cli.main(["next", "--owner", "   "])

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"] == "invalid_arguments"


def test_consumer_manual_enforces_safe_workflow():
    """消费者手册只描述阶段化 send-final 与修复流程。"""
    text = (ROOT / "skill" / "queue_consumer.md").read_text(encoding="utf-8")

    for required in (
        "会话隔离", "FIFO", "build_context.py", "online_fetch.py",
        "--job-id", "--owner", "browser-use", "renew", "send-final",
        "repair-record", "reconcile", "uncertain",
    ):
        assert required in text
    assert text.index("build_context.py") < text.index("online_fetch.py")
    assert "reply_and_record.py" not in text
    assert "final-claim" not in text
    assert "send-final` 返回后不得再执行 `fail`" in text


def test_loop_manual_defaults_to_queue_and_keeps_emergency():
    """loop 默认转交队列消费者，并保留隔离的应急流程。"""
    text = (ROOT / "skill" / "loop_prompt.md").read_text(encoding="utf-8")

    assert "skill/queue_consumer.md" in text
    assert "应急模式" in text
    assert text.index("queue_consumer.md") < text.index("应急模式")
    assert text.index("应急模式") < text.index("brain_cycle.py")
    assert "send-final" in text[:text.index("应急模式")]
    assert "reply_and_record.py" not in text


def test_task6_plan_describes_actual_staged_send_workflow():
    """The approved plan no longer directs consumers through legacy send."""
    plan = (
        ROOT / "docs" / "superpowers" / "plans"
        / "2026-07-10-reliable-scheduler-online-response.md"
    ).read_text(encoding="utf-8")
    task6 = plan.split("### Task 6:", 1)[1].split("### Task 7:", 1)[0]

    assert "send-final" in task6
    assert "repair-record" in task6
    assert "reconcile" in task6
    assert "reply_and_record.py" not in task6


def test_uncertain_final_failure_is_persisted_without_reclaim(
    tmp_path
):
    """Unknown send results must freeze automatic final retries."""
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue({"conv_id": "u1", "mid": 1}, detected_at=100)
    assert db.claim("cursor", 500, 1, 100)
    record = {
        "conv_id": "u1", "mid": 1, "reply": "private",
        "markdown": False, "bot_uid": 7, "created_at": 1_000,
    }
    assert ConsumerQueue(db).prepare_final(
        job_id, "cursor", record, 1_000, 120
    )
    assert Outbox(db).fail_final(
        job_id, "cursor", 1_100, "ProcessCrashed", 61_000,
        uncertain=True,
    )
    assert Outbox(db).state(job_id, "final")["state"] == "uncertain"
    assert db.get(job_id)["status"] == "retry_wait"
    assert db.claim("cursor-retry", 61_000, 1, 120) == []
