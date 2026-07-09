# app/storage.py
"""接收器落盘:按会话追加 JSONL、维护 seen_mids 去重集、可选原始 payload 转储。"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _conv_path(data_dir: str, conv_id: str) -> Path:
    return Path(data_dir) / "conversations" / f"{conv_id}.jsonl"


def append_message(data_dir: str, conv_id: str, record: dict) -> None:
    """把一条记录追加到会话 JSONL;ensure_ascii=False 以保留中文原文。"""
    p = _conv_path(data_dir, conv_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _seen_path(data_dir: str) -> Path:
    return Path(data_dir) / "seen_mids.json"


def load_seen_mids(data_dir: str) -> set:
    """读取去重集;文件缺失或损坏时返回空集,保证接收器不因坏状态而崩溃。"""
    p = _seen_path(data_dir)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, ValueError):
        return set()


def save_seen_mids(data_dir: str, mids: set) -> None:
    """持久化去重集;排序并剔除 None 以获得稳定、干净的输出。"""
    p = _seen_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(m for m in mids if m is not None)), encoding="utf-8")


def dump_raw(data_dir: str, mid, payload: dict) -> None:
    """转储原始 payload 到 raw/<ts>_<mid>.json;用于 Phase 0 核对真实字段结构。"""
    d = Path(data_dir) / "raw"
    d.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    (d / f"{ts}_{mid}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
