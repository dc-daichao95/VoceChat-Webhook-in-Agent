#!/usr/bin/env python3
"""Run and inspect the independent reliable scheduler service."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
# Insert the repository root ahead of this script's own directory so
# ``import scheduler`` resolves to the package, never to this module file.
sys.path.insert(0, str(REPO))

from scheduler.db import QueueDB  # noqa: E402
from scheduler.service import (  # noqa: E402
    AlreadyRunningError,
    SchedulerConfig,
    SchedulerService,
    read_health,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reliable scheduler service")
    parser.add_argument("--db")
    parser.add_argument("--state")
    parser.add_argument("--inbound")
    parser.add_argument("--health")
    parser.add_argument("--lock")
    # ``status`` is an alias of ``health`` retained for operator ergonomics and
    # the Task 8 lifecycle scripts.
    parser.add_argument(
        "command", choices=("run", "once", "health", "status", "init-db")
    )
    return parser


def _share_values(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def _runtime_environment(
    environ: Optional[Mapping[str, str]]
) -> Dict[str, str]:
    if environ is not None:
        return dict(environ)
    load_dotenv(REPO / ".env")
    values = dict(os.environ)
    share = _share_values(REPO / "share.env")
    aliases = {
        "WEBDAV_URL": "url",
        "WEBDAV_USER": "user",
        "WEBDAV_PASSWORD": "passwd",
    }
    for target, source in aliases.items():
        if target not in values and source in share:
            values[target] = share[source]
    return values


def _local_path(raw: Optional[str], fallback: Path) -> Path:
    return Path(raw) if raw else fallback


def _service_config(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> SchedulerConfig:
    config = SchedulerConfig.from_mapping(environ, REPO)
    return replace(
        config,
        db_path=_local_path(args.db, config.db_path),
        state_path=_local_path(args.state, config.state_path),
        inbound_dir=_local_path(args.inbound, config.inbound_dir),
        health_path=_local_path(args.health, config.health_path),
        lock_path=_local_path(args.lock, config.lock_path),
    )


def _write(value: dict) -> None:
    print(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True, allow_nan=False,
        )
    )


def main(
    argv: Optional[List[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """Execute one scheduler lifecycle command with sanitized output."""
    args = _parser().parse_args(argv)
    default_db = REPO / "data" / "queue.db"
    default_health = REPO / "data" / "scheduler-health.json"
    if args.command == "init-db":
        QueueDB(_local_path(args.db, default_db))
        _write({"status": "initialized"})
        return EXIT_OK
    if args.command in ("health", "status"):
        _write(read_health(_local_path(args.health, default_health)))
        return EXIT_OK
    try:
        config = _service_config(args, _runtime_environment(environ))
    except (TypeError, ValueError):
        _write({"error": "config_error"})
        return EXIT_CONFIG
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    service = SchedulerService(config)
    try:
        if args.command == "once":
            # ``once`` bypasses the single-instance lock; do not run it while a
            # ``run`` daemon is active or both may write state.json concurrently.
            _write({"status": "ok", "health": service.tick().last_tick_at})
        else:
            service.run_forever()
    except AlreadyRunningError:
        _write({"error": "already_running"})
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
