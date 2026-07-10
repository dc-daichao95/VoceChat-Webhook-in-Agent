#!/usr/bin/env python3
"""从命令行执行有界 HTTP 快路径，并可立即把证据追加到队列。"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scheduler.consumer import ConsumerQueue  # noqa: E402
from scheduler.db import QueueDB  # noqa: E402
from scheduler.online import gather_progressively  # noqa: E402

DEFAULT_DB = Path(os.environ.get("SCHEDULER_DB", REPO / "data" / "queue.db"))


class EvidenceLeaseRejected(RuntimeError):
    """标识证据写入时任务 owner 或租约已失效。"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="执行有总预算和响应大小上限的 HTTP 快路径"
    )
    parser.add_argument("kind", choices=("json", "text"))
    parser.add_argument("url")
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--owner")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--max-chars", type=int, default=4000)
    return parser


def _source(args: argparse.Namespace) -> Dict[str, object]:
    source: Dict[str, object] = {
        "kind": args.kind,
        "url": args.url,
        "timeout": args.timeout,
    }
    if args.kind == "text":
        source["max_chars"] = args.max_chars
    return source


def _appender(
    args: argparse.Namespace,
) -> Optional[Callable[[Dict[str, object]], None]]:
    if args.job_id is None:
        return None
    consumer = ConsumerQueue(QueueDB(args.db))

    def append(evidence: Dict[str, object]) -> None:
        stored = consumer.append_evidence_owned(
            args.job_id,
            evidence,
            args.owner,
            now=int(time.time() * 1000),
        )
        if not stored:
            raise EvidenceLeaseRejected()

    return append


def _exit_code(result: Dict[str, object]) -> int:
    errors = result["errors"]
    if any(item.get("stage") == "persist" for item in errors):
        return 1
    if result["status"] != "failed" or result["fallback"] == "browser":
        return 0
    return 1


def _failure_result(stage: str, error: BaseException) -> Dict[str, object]:
    """构造不含异常正文、参数、路径或 URL 的稳定失败结果。"""
    return {
        "status": "failed",
        "evidence": [],
        "errors": [
            {
                "source": "cli",
                "stage": stage,
                "error": type(error).__name__,
            }
        ],
        "fallback": None,
        "deadline_reached": False,
        "deadline_stage": None,
        "attempted": 0,
        "persisted": 0,
    }


def _write_result(result: Dict[str, object]) -> None:
    """只输出符合标准 JSON 的有限数值结果。"""
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _write_failure(stage: str, error: BaseException) -> int:
    _write_result(_failure_result(stage, error))
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    """解析参数、执行单来源渐进抓取并输出安全 JSON 结果。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout):
        parser.error("--timeout must be finite")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.max_chars <= 0:
        parser.error("--max-chars must be positive")
    if args.job_id is not None and args.job_id <= 0:
        parser.error("--job-id must be positive")
    if args.job_id is not None and (
        not isinstance(args.owner, str) or not args.owner.strip()
    ):
        parser.error("--owner is required with --job-id")
    if args.job_id is None and args.owner is not None:
        parser.error("--owner requires --job-id")
    # CLI 是最终脱敏边界，未知底层异常也不得形成 traceback。
    try:
        appender = _appender(args)
    except Exception as error:
        return _write_failure("database", error)
    try:
        result = gather_progressively(
            [_source(args)],
            deadline=time.monotonic() + args.timeout,
            append_evidence=appender,
        )
    except Exception as error:
        return _write_failure("execute", error)
    try:
        exit_code = _exit_code(result)
        _write_result(result)
    except Exception as error:
        return _write_failure("output", error)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
