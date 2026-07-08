# app/storage.py
from __future__ import annotations

import json
import time
from pathlib import Path


def _conv_path(data_dir: str, conv_id: str) -> Path:
    return Path(data_dir) / "conversations" / f"{conv_id}.jsonl"


def append_message(data_dir: str, conv_id: str, record: dict) -> None:
    p = _conv_path(data_dir, conv_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _seen_path(data_dir: str) -> Path:
    return Path(data_dir) / "seen_mids.json"


def load_seen_mids(data_dir: str) -> set:
    p = _seen_path(data_dir)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, ValueError):
        return set()


def save_seen_mids(data_dir: str, mids: set) -> None:
    p = _seen_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(m for m in mids if m is not None)), encoding="utf-8")


def dump_raw(data_dir: str, mid, payload: dict) -> None:
    d = Path(data_dir) / "raw"
    d.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    (d / f"{ts}_{mid}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
