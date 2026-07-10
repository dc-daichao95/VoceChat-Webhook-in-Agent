#!/usr/bin/env python3
"""以稳定脱敏 JSON 操作 Cursor 消费者队列。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scheduler.consumer import ConsumerQueue, JOB_STATUSES  # noqa: E402
from scheduler.db import QueueDB  # noqa: E402
from scheduler.errors import safe_error_category  # noqa: E402
from scheduler.final_delivery import repair_record, send_final  # noqa: E402
from scheduler.outbox import Outbox  # noqa: E402
from scheduler.policy import retry_delay_seconds  # noqa: E402


DEFAULT_DB = Path(os.environ.get("SCHEDULER_DB", REPO / "data" / "queue.db"))
DEFAULT_LEASE_SECONDS = 120
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 3


class InputError(ValueError):
    """Represent local CLI input failures without exposing details."""


class JsonArgumentParser(argparse.ArgumentParser):
    """把 argparse 错误转为由 main 脱敏输出的异常。"""

    def error(self, message: str) -> None:
        _write({"command": "usage", "error": "invalid_arguments", "ok": False})
        raise SystemExit(2)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write(value: Dict[str, Any]) -> None:
    print(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True, allow_nan=False,
        )
    )


def _result(command: str, job_id: int, ok: bool) -> Dict[str, Any]:
    value: Dict[str, Any] = {"command": command, "ok": ok}
    if ok:
        value["job_id"] = job_id
    if not ok:
        value["error"] = "transition_rejected"
    return value


def _safe_error(error: str) -> str:
    return safe_error_category(error)


def _positive(parser: argparse.ArgumentParser, name: str, value: int) -> None:
    if value <= 0:
        parser.error("{} must be positive".format(name))


def _add_job_owner(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--job-id", type=int, required=True)
    subparser.add_argument("--owner", required=True)


def _build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(description="Cursor durable queue consumer")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    commands = parser.add_subparsers(dest="command", required=True)

    next_parser = commands.add_parser("next")
    next_parser.add_argument("--owner", required=True)
    next_parser.add_argument("--limit", type=int, default=3)

    for name in ("renew", "cancel"):
        _add_job_owner(commands.add_parser(name))

    sending = commands.add_parser("send-final")
    _add_job_owner(sending)
    sending.add_argument("--reply-file", required=True)
    sending.add_argument("--markdown", action="store_true")

    repair = commands.add_parser("repair-record")
    repair.add_argument("--job-id", type=int, required=True)

    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--job-id", type=int, required=True)
    reconcile.add_argument(
        "--action", choices=("mark-done", "cancel", "retry"), required=True
    )
    reconcile.add_argument("--confirm", action="store_true")
    reconcile.add_argument("--confirm-duplicate-risk", action="store_true")

    evidence = commands.add_parser("evidence")
    _add_job_owner(evidence)
    evidence.add_argument("--file", required=True)

    mode = commands.add_parser("mode")
    _add_job_owner(mode)
    mode.add_argument(
        "--value", choices=("unknown", "none", "fast_http", "browser"),
        required=True,
    )

    fail = commands.add_parser("fail")
    _add_job_owner(fail)
    fail.add_argument("--error", required=True)
    fail.add_argument("--uncertain", action="store_true")

    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=JOB_STATUSES)
    return parser


def _claimed_event(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event": "job",
        "id": job["id"],
        "conv_id": job["conv_id"],
        "mid": job["mid"],
        "status": job["status"],
        "attempts": job["attempts"],
        "network_mode": job["network_mode"],
        "lease_until": job["lease_until"],
        "ok": True,
    }


def _list_event(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event": "job",
        "id": job["id"],
        "conv_id": job["conv_id"],
        "mid": job["mid"],
        "status": job["status"],
        "attempts": job["attempts"],
        "network_mode": job["network_mode"],
        "lease_until": job["lease_until"],
        "available_at": job["available_at"],
        "final_delivery_state": job["final_delivery_state"],
        "block_reason": job["block_reason"],
        "record_pending": job["record_pending"],
    }


def _next(db: QueueDB, args: argparse.Namespace, now: int) -> int:
    jobs = db.claim(
        args.owner, now, args.limit, DEFAULT_LEASE_SECONDS
    )
    if not jobs:
        _write({"event": "empty"})
        return EXIT_OK
    for job in jobs:
        _write(_claimed_event(job))
    return EXIT_OK


def _renew(
    consumer: ConsumerQueue, args: argparse.Namespace, now: int
) -> int:
    ok = consumer.renew(
        args.job_id, args.owner, now, DEFAULT_LEASE_SECONDS
    )
    _write(_result("renew", args.job_id, ok))
    return EXIT_OK if ok else EXIT_CONFLICT


def _read_evidence(path: str) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InputError() from error
    if not isinstance(value, dict):
        raise InputError()
    return value


def _evidence(
    consumer: ConsumerQueue, args: argparse.Namespace, now: int
) -> int:
    evidence = _read_evidence(args.file)
    ok = consumer.append_evidence_owned(
        args.job_id, evidence, args.owner, now
    )
    _write(_result("evidence", args.job_id, ok))
    return EXIT_OK if ok else EXIT_CONFLICT


def _mode(db: QueueDB, args: argparse.Namespace, now: int) -> int:
    ok = db.set_network_mode(
        args.job_id, args.owner, args.value, now
    )
    _write(_result("mode", args.job_id, ok))
    return EXIT_OK if ok else EXIT_CONFLICT


def _send_final(db: QueueDB, args: argparse.Namespace, now: int) -> int:
    try:
        reply = Path(args.reply_file).read_text(encoding="utf-8")
    except OSError as error:
        raise InputError() from error
    load_dotenv(REPO / ".env")
    server = os.environ.get("VOCECHAT_SERVER_URL")
    api_key = os.environ.get("VOCECHAT_API_KEY")
    if not server or not api_key:
        return _failure("send-final", "config_error")
    result = send_final(
        db, args.job_id, args.owner, reply, args.markdown,
        server, api_key, clock=_now_ms,
    )
    _write(result)
    return EXIT_OK if result["status"] == "done" else EXIT_CONFLICT


def _repair(db: QueueDB, args: argparse.Namespace, now: int) -> int:
    repaired = repair_record(db, args.job_id, now)
    _write({
        "command": "repair-record", "job_id": args.job_id, "ok": repaired
    })
    return EXIT_OK if repaired else EXIT_CONFLICT


def _reconcile(
    consumer: ConsumerQueue, args: argparse.Namespace, now: int
) -> int:
    if not args.confirm or (
        args.action == "retry" and not args.confirm_duplicate_risk
    ):
        return _failure(
            "reconcile", "confirmation_required", EXIT_CONFLICT
        )
    status = consumer.reconcile(
        args.job_id, args.action, now, args.confirm_duplicate_risk
    )
    if status is None:
        return _failure("reconcile", "transition_rejected", EXIT_CONFLICT)
    _write({"command": "reconcile", "job_id": args.job_id, "status": status})
    return EXIT_OK


def _fail(
    db: QueueDB, outbox: Outbox, args: argparse.Namespace, now: int
) -> int:
    job = db.get(args.job_id)
    delay_ms = retry_delay_seconds(job["attempts"]) * 1000
    error = _safe_error(args.error)
    final = outbox.state(args.job_id, "final")
    if final is not None and final["state"] == "claimed":
        ok = outbox.fail_final(
            args.job_id,
            args.owner,
            now,
            error,
            now + delay_ms,
            uncertain=args.uncertain,
        )
    elif not args.uncertain and (
        final is None or final["state"] == "failed"
    ):
        ok = db.fail(
            args.job_id, args.owner, error, now + delay_ms, now
        )
    else:
        ok = False
    _write(_result("fail", args.job_id, ok))
    return EXIT_OK if ok else EXIT_CONFLICT


def _cancel(
    consumer: ConsumerQueue, args: argparse.Namespace, now: int
) -> int:
    ok = consumer.cancel(args.job_id, args.owner, now)
    _write(_result("cancel", args.job_id, ok))
    return EXIT_OK if ok else EXIT_CONFLICT


def _list(consumer: ConsumerQueue, args: argparse.Namespace) -> int:
    jobs = consumer.list_jobs(args.status)
    if not jobs:
        _write({"event": "empty"})
        return EXIT_OK
    for job in jobs:
        _write(_list_event(job))
    return EXIT_OK


def _dispatch(
    db: QueueDB, args: argparse.Namespace, now: int
) -> int:
    consumer = ConsumerQueue(db)
    outbox = Outbox(db)
    if args.command == "next":
        return _next(db, args, now)
    if args.command == "renew":
        return _renew(consumer, args, now)
    if args.command == "evidence":
        return _evidence(consumer, args, now)
    if args.command == "mode":
        return _mode(db, args, now)
    if args.command == "send-final":
        return _send_final(db, args, now)
    if args.command == "repair-record":
        return _repair(db, args, now)
    if args.command == "reconcile":
        return _reconcile(consumer, args, now)
    if args.command == "fail":
        return _fail(db, outbox, args, now)
    if args.command == "cancel":
        return _cancel(consumer, args, now)
    return _list(consumer, args)


def _failure(command: str, error: str, code: int = EXIT_ERROR) -> int:
    _write({"command": command, "error": error, "ok": False})
    return code


def main(argv: Optional[List[str]] = None) -> int:
    """执行一个队列命令；所有非帮助结果只写脱敏 JSON 到 stdout。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "job_id"):
        _positive(parser, "--job-id", args.job_id)
    if hasattr(args, "limit"):
        _positive(parser, "--limit", args.limit)
    if hasattr(args, "owner") and (
        not isinstance(args.owner, str) or not args.owner.strip()
    ):
        parser.error("--owner must not be blank")
    try:
        db = QueueDB(args.db)
        return _dispatch(db, args, _now_ms())
    except (sqlite3.Error, OSError):
        return _failure(args.command, "database_error")
    except InputError:
        return _failure(args.command, "input_error", EXIT_CONFLICT)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _failure(args.command, "invalid_input", EXIT_CONFLICT)
    except Exception:
        return _failure(args.command, "internal_error")


if __name__ == "__main__":
    raise SystemExit(main())
