#!/usr/bin/env python3
"""打印某会话组装后的回复上下文(供大脑在 /loop 步骤 2a 读取)。

输出 = 用户事实卡片 + 早期对话摘要 + 最近 N 条逐字,取代全量读历史。
用法:python scripts/build_context.py --conv u1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain import context  # noqa: E402

HISTORY_DIR = REPO / "data" / "history"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conv", required=True, help="会话 id,如 u2 / g5")
    args = ap.parse_args(argv)
    print(context.render(context.build_context(args.conv, str(HISTORY_DIR))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
