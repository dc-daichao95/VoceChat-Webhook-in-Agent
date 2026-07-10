"""把已下载的会话 JSONL 记录纯入队到 SQLite 持久队列。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from brain import context
from scheduler.db import QueueDB


def _valid_cursor(last_processed_mid: Any) -> int:
    if type(last_processed_mid) is int:
        return last_processed_mid
    return -1


def _eligible_records(
    records: list, last_processed_mid: Any, conv_id: Optional[str]
) -> List[Dict[str, Any]]:
    cursor = _valid_cursor(last_processed_mid)
    eligible = []
    for record in records:
        if not isinstance(record, dict) or record.get("direction") != "in":
            continue
        mid = record.get("mid")
        if type(mid) is not int or mid <= cursor:
            continue
        if conv_id is not None and record.get("conv_id") != conv_id:
            continue
        eligible.append(record)
    return sorted(eligible, key=lambda record: record["mid"])


def _enqueue_records(
    db: QueueDB,
    records: list,
    last_processed_mid: Any,
    detected_at: int,
    conv_id: Optional[str],
) -> List[Tuple[int, bool]]:
    return [
        db.enqueue_with_created(record, detected_at)
        for record in _eligible_records(records, last_processed_mid, conv_id)
    ]


def enqueue_new_records(
    db: QueueDB,
    records: list,
    last_processed_mid: Any,
    detected_at: int,
    conv_id: Optional[str] = None,
) -> List[int]:
    """过滤并按 mid 升序幂等入队；非法游标按 -1，返回稳定任务 ID。"""
    results = _enqueue_records(
        db, records, last_processed_mid, detected_at, conv_id
    )
    return [job_id for job_id, _ in results]


def ingest_downloaded_conversations(
    db: QueueDB,
    inbound_dir: Union[str, Path],
    state: dict,
    detected_at: int,
) -> int:
    """按文件名摄取下载会话，仅返回本轮首次创建的任务数。"""
    inbound_path = Path(inbound_dir)
    if not inbound_path.exists():
        return 0
    conversations = state.get("conversations", {})
    created_count = 0
    for path in sorted(inbound_path.glob("*.jsonl"), key=lambda item: item.name):
        conv_id = path.stem
        conversation = conversations.get(conv_id, {})
        cursor = conversation.get("last_processed_mid", -1)
        results = _enqueue_records(
            db, context.read_jsonl(path), cursor, detected_at, conv_id
        )
        created_count += sum(created for _, created in results)
    return created_count
