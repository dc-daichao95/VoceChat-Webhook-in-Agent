# tests/test_storage.py
import json
from pathlib import Path

from app import storage


def test_append_message_creates_and_appends(tmp_path):
    storage.append_message(str(tmp_path), "u7910", {"mid": 1, "content": "a"})
    storage.append_message(str(tmp_path), "u7910", {"mid": 2, "content": "b"})
    p = tmp_path / "conversations" / "u7910.jsonl"
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["mid"] == 1
    assert json.loads(lines[1])["content"] == "b"


def test_seen_mids_roundtrip(tmp_path):
    assert storage.load_seen_mids(str(tmp_path)) == set()
    storage.save_seen_mids(str(tmp_path), {3, 1, 2})
    assert storage.load_seen_mids(str(tmp_path)) == {1, 2, 3}


def test_load_seen_mids_corrupt_returns_empty(tmp_path):
    (tmp_path / "seen_mids.json").write_text("not-json", encoding="utf-8")
    assert storage.load_seen_mids(str(tmp_path)) == set()


def test_dump_raw_writes_file(tmp_path):
    storage.dump_raw(str(tmp_path), 42, {"mid": 42, "x": 1})
    files = list((tmp_path / "raw").glob("*_42.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["x"] == 1
