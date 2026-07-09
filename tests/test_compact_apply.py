import gzip
import json
from pathlib import Path

import compact


def _seed(hist: Path, conv: str, n: int):
    hist.mkdir(parents=True, exist_ok=True)
    (hist / f"{conv}.jsonl").write_text(
        "\n".join(json.dumps({"mid": i, "content": f"m{i}"}) for i in range(n)) + "\n",
        encoding="utf-8",
    )


def test_archive_and_truncate(tmp_path):
    hist = tmp_path / "history"
    arch = tmp_path / "archive"
    _seed(hist, "u1", 50)
    old_count = compact.archive_and_truncate("u1", str(hist), str(arch), keep=20)
    assert old_count == 30
    remaining = [json.loads(l) for l in (hist / "u1.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(remaining) == 20 and remaining[0]["mid"] == 30
    gzs = list((arch / "u1").glob("*.jsonl.gz"))
    assert len(gzs) == 1
    with gzip.open(gzs[0], "rt", encoding="utf-8") as f:
        archived = [json.loads(l) for l in f if l.strip()]
    assert len(archived) == 30 and archived[-1]["mid"] == 29


def test_archive_and_truncate_under_keep_noop(tmp_path):
    hist = tmp_path / "history"
    arch = tmp_path / "archive"
    _seed(hist, "u2", 10)
    assert compact.archive_and_truncate("u2", str(hist), str(arch), keep=20) == 0
    assert not arch.exists() or not list(arch.glob("**/*.gz"))
