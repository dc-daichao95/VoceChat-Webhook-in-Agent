"""下载会话 JSONL 入 SQLite 队列的行为测试。"""

import json
import sqlite3
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scheduler.db import QueueDB
from scheduler.ingest import enqueue_new_records, ingest_downloaded_conversations


def make_record(mid, conv_id="c1", direction="in"):
    """构造最小下载记录。"""
    return {
        "conv_id": conv_id,
        "mid": mid,
        "direction": direction,
        "content": "message-{}".format(mid),
    }


def write_jsonl(path, records, bad_lines=()):
    """写入测试 JSONL，可混入损坏行。"""
    lines = [json.dumps(record) for record in records]
    lines.extend(bad_lines)
    path.write_text("\n".join(lines), encoding="utf-8")


def test_enqueue_filters_invalid_records_and_sorts_by_mid(tmp_path):
    """只按 mid 顺序入队符合方向、类型和游标约束的记录。"""
    db = QueueDB(tmp_path / "queue.db")
    records = [
        make_record(5),
        make_record(3),
        make_record(6, direction="out"),
        make_record(None),
        make_record(True),
        make_record(2),
    ]

    ids = enqueue_new_records(db, records, last_processed_mid=2, detected_at=100)

    assert [db.get(job_id)["mid"] for job_id in ids] == [3, 5]


@pytest.mark.parametrize("cursor", (None, True, "4"))
def test_enqueue_treats_invalid_cursor_as_minus_one(tmp_path, cursor):
    """非 int 或 bool 游标按 -1 处理。"""
    db = QueueDB(tmp_path / "queue-{}.db".format(repr(cursor)))

    ids = enqueue_new_records(
        db, [make_record(0)], last_processed_mid=cursor, detected_at=100
    )

    assert [db.get(job_id)["mid"] for job_id in ids] == [0]


def test_enqueue_rejects_records_from_other_or_missing_conversation(tmp_path):
    """指定会话时拒绝跨会话及缺少 conv_id 的记录。"""
    db = QueueDB(tmp_path / "queue.db")
    missing_conv = make_record(2)
    missing_conv.pop("conv_id")

    ids = enqueue_new_records(
        db,
        [make_record(1, "other"), missing_conv, make_record(3, "expected")],
        last_processed_mid=-1,
        detected_at=100,
        conv_id="expected",
    )

    assert [db.get(job_id)["mid"] for job_id in ids] == [3]


def test_enqueue_is_idempotent_and_preserves_first_detection(tmp_path):
    """重复候选返回稳定 ID，且首次检测时间不被覆盖。"""
    db = QueueDB(tmp_path / "queue.db")
    records = [make_record(2), make_record(1)]

    first_ids = enqueue_new_records(db, records, -1, 100)
    second_ids = enqueue_new_records(db, records, -1, 200)

    assert second_ids == first_ids
    assert [db.get(job_id)["detected_at"] for job_id in first_ids] == [100, 100]


def test_concurrent_enqueue_has_one_creator_and_preserves_its_detection(tmp_path):
    """并发幂等入队只创建一行，并保留获胜方首次检测时间。"""
    path = tmp_path / "queue.db"
    QueueDB(path)
    barrier = Barrier(2)
    payload = make_record(1, "concurrent")

    def enqueue(detected_at):
        db = QueueDB(path)
        barrier.wait(timeout=5)
        job_id, created = db.enqueue_with_created(payload, detected_at)
        return detected_at, job_id, created

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enqueue, (100, 200)))

    assert results[0][1] == results[1][1]
    creators = [result for result in results if result[2]]
    assert len(creators) == 1
    with sqlite3.connect(str(path)) as connection:
        count, detected_at = connection.execute(
            "SELECT COUNT(*), detected_at FROM jobs"
        ).fetchone()
    assert count == 1
    assert detected_at == creators[0][0]


def test_ingest_uses_per_conversation_cursors_and_counts_new_jobs(tmp_path):
    """目录摄取按文件会话游标过滤，并只统计首次新增任务。"""
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    write_jsonl(inbound / "b.jsonl", [make_record(2, "b"), make_record(3, "b")])
    write_jsonl(inbound / "a.jsonl", [make_record(1, "a"), make_record(2, "a")])
    state = {
        "conversations": {
            "a": {"last_processed_mid": 1},
            "b": {"last_processed_mid": 2},
        }
    }
    original_state = deepcopy(state)
    db = QueueDB(tmp_path / "queue.db")

    assert ingest_downloaded_conversations(db, inbound, state, 100) == 2
    assert ingest_downloaded_conversations(db, inbound, state, 200) == 0
    a_job = db.find("a", 2)
    b_job = db.find("b", 3)
    assert a_job["detected_at"] == 100
    assert b_job["detected_at"] == 100
    assert a_job["id"] < b_job["id"]
    assert state == original_state


def test_ingest_rejects_conv_mismatch_and_skips_bad_json(tmp_path):
    """坏行不阻断正常行，文件 stem 必须与记录 conv_id 一致。"""
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    missing_conv = make_record(2, "file")
    missing_conv.pop("conv_id")
    lines = (
        json.dumps(make_record(3, "file")),
        "{bad json",
        json.dumps(make_record(4, "file")),
        json.dumps(make_record(1, "other")),
        json.dumps(missing_conv),
    )
    (inbound / "file.jsonl").write_text("\n".join(lines), encoding="utf-8")
    db = QueueDB(tmp_path / "queue.db")

    created = ingest_downloaded_conversations(db, inbound, {}, 100)

    assert created == 2
    assert db.find("file", 3) is not None
    assert db.find("file", 4) is not None
    assert db.find("other", 1) is None


def test_ingest_missing_directory_returns_zero(tmp_path):
    """下载目录不存在时无任务可入队。"""
    db = QueueDB(tmp_path / "queue.db")

    assert ingest_downloaded_conversations(
        db, tmp_path / "missing", {}, detected_at=100
    ) == 0


def test_ingest_ignores_non_jsonl_files(tmp_path):
    """即使其他扩展名包含有效记录，也不得将其作为会话摄取。"""
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    write_jsonl(inbound / "ignored.txt", [make_record(1, "ignored")])
    db = QueueDB(tmp_path / "queue.db")

    assert ingest_downloaded_conversations(db, inbound, {}, 100) == 0
    assert db.find("ignored", 1) is None
