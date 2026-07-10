"""Idempotent cross-process history and state recording."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union


ROOT = Path(__file__).resolve().parent.parent
PathLike = Union[str, os.PathLike]


def _lock_once(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _file_lock(path: Path, timeout: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                _lock_once(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("record lock timeout")
                time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(handle)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _find_inbound(path: Path, mid: int) -> Optional[Dict[str, Any]]:
    for record in _read_jsonl(path):
        if record.get("mid") == mid:
            return record
    return None


def _outbound(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mid": None,
        "conv_id": record["conv_id"],
        "direction": "out",
        "from_uid": record["bot_uid"],
        "content_type": (
            "text/markdown" if record["markdown"] else "text/plain"
        ),
        "content": record["reply"],
        "in_reply_to": record["mid"],
        "created_at": record["created_at"],
        "recorded_at": record["created_at"],
    }


def _update_history(
    record: Dict[str, Any], inbound_dir: Path, history_dir: Path
) -> None:
    conv_id, mid = record["conv_id"], record["mid"]
    history_path = history_dir / "{}.jsonl".format(conv_id)
    records = _read_jsonl(history_path)
    if not any(item.get("mid") == mid for item in records):
        inbound = _find_inbound(inbound_dir / "{}.jsonl".format(conv_id), mid)
        if inbound is not None:
            records.append(inbound)
    if not any(
        item.get("direction") == "out" and item.get("in_reply_to") == mid
        for item in records
    ):
        records.append(_outbound(record))
    encoded = "".join(
        json.dumps(item, ensure_ascii=False) + "\n" for item in records
    )
    _atomic_text(history_path, encoded)


def _update_state(record: Dict[str, Any], state_file: Path) -> None:
    state = _read_json(state_file, {"conversations": {}, "seen_mids": []})
    conversations = state.setdefault("conversations", {})
    current = conversations.setdefault(record["conv_id"], {})
    current["last_processed_mid"] = max(
        current.get("last_processed_mid", -1), record["mid"]
    )
    current["last_processed_at"] = record["created_at"]
    seen = set(state.get("seen_mids", []))
    seen.add(record["mid"])
    state["seen_mids"] = sorted(seen)
    _atomic_text(
        state_file, json.dumps(state, ensure_ascii=False, indent=2)
    )


def record_reply(
    record: Dict[str, Any],
    *,
    state_file: PathLike = ROOT / "data" / "state.json",
    inbound_dir: PathLike = ROOT / "data" / "inbound",
    history_dir: PathLike = ROOT / "data" / "history",
    lock_file: PathLike = ROOT / "data" / "record.lock",
    lock_timeout: float = 10,
) -> None:
    """Record one sent reply without sending; retries are idempotent."""
    with _file_lock(Path(lock_file), lock_timeout):
        _update_history(record, Path(inbound_dir), Path(history_dir))
        _update_state(record, Path(state_file))
