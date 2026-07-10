#!/usr/bin/env python3
"""检测并压缩超长会话历史:归档旧记录(gzip)、活跃 JSONL 截到最近 N。

摘要/卡片由大脑在 loop 内用 Write 更新;本脚本只做检测、归档、截断、落盘。
用法:
  python scripts/compact.py --check [--conv u1]
  python scripts/compact.py --apply --conv u1
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain import compaction  # noqa: E402
from brain.context import read_jsonl  # noqa: E402

HISTORY_DIR = REPO / "data" / "history"
ARCHIVE_DIR = REPO / "data" / "archive"


def archive_and_truncate(conv_id: str, history_dir: str, archive_dir: str, keep: int = compaction.RECENT_KEEP) -> int:
    """原子归档:先写 gzip 成功,再把活跃 JSONL 重写为最近 keep 条。返回归档条数。"""
    jsonl = Path(history_dir) / f"{conv_id}.jsonl"
    old, recent = compaction.split_recent(read_jsonl(jsonl), keep=keep)
    if not old:
        return 0
    out_dir = Path(archive_dir) / conv_id
    out_dir.mkdir(parents=True, exist_ok=True)
    gz = out_dir / f"{int(time.time() * 1000)}.jsonl.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as f:
        for r in old:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 归档成功后才截断,保证失败不丢数据。
    tmp = jsonl.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recent), encoding="utf-8")
    tmp.replace(jsonl)
    return len(old)


def _iter_convs(history_dir: Path):
    for p in sorted(history_dir.glob("*.jsonl")):
        yield p.stem


def cmd_check(args) -> int:
    if not HISTORY_DIR.exists():
        print("COMPACT_NONE")
        return 0
    convs = [args.conv] if args.conv else list(_iter_convs(HISTORY_DIR))
    pending = [(c, len(read_jsonl(HISTORY_DIR / f"{c}.jsonl"))) for c in convs]
    pending = [(c, n) for c, n in pending if compaction.needs_compaction(n)]
    if not pending:
        print("COMPACT_NONE")
        return 0
    for conv, n in pending:
        print(f"COMPACT_NEEDED {conv} raw={n} keep={compaction.RECENT_KEEP}")
    return 0


def cmd_apply(args) -> int:
    n = archive_and_truncate(args.conv, str(HISTORY_DIR), str(ARCHIVE_DIR))
    print(f"[OK] {args.conv}: archived {n} records; active kept {compaction.RECENT_KEEP}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--conv")
    args = ap.parse_args(argv)
    if args.apply:
        if not args.conv:
            print("--apply requires --conv", file=sys.stderr)
            return 2
        return cmd_apply(args)
    return cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
