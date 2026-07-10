#!/usr/bin/env python3
"""发送一条回复并记账(大脑 loop 的"发送+记账"步骤,编码安全)。

回复文本从 UTF-8 文件读取(避免 Windows 控制台参数编码问题)。
发送成功后:把对应入站记录 + 出站记录追加到 data/history/<conv>.jsonl,
并更新 data/state.json(last_processed_mid、seen_mids)。发送失败则不记账、退出非 0。

用法:
  python scripts/reply_and_record.py --conv u2 --mid 1313 --reply-file data/_reply.txt
  python scripts/reply_and_record.py --conv g5 --mid 900 --reply-file data/_reply.txt --markdown
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

import send  # noqa: E402
from brain.recording import record_reply  # noqa: E402


def _parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--conv", required=True, help="会话 id,如 u2 / g5")
    ap.add_argument("--mid", required=True, type=int, help="要回复的入站消息 mid")
    ap.add_argument("--reply-file", required=True, help="UTF-8 回复文本文件")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--allow-legacy-send", action="store_true")
    return ap.parse_args(argv)


def _send_legacy(server, key, conv, reply, markdown):
    kind = conv[0]
    target_id = int(conv[1:])
    if kind == "u":
        return send.send_message(
            server, key, reply, uid=target_id, markdown=markdown
        )
    if kind == "g":
        return send.send_message(
            server, key, reply, gid=target_id, markdown=markdown
        )
    raise ValueError("unknown conversation prefix")


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.allow_legacy_send:
        print(
            "deprecated direct sender refused; use queue_cli send-final or "
            "pass --allow-legacy-send explicitly",
            file=sys.stderr,
        )
        return 2
    print(
        "DEPRECATED: legacy send bypasses durable final reconciliation",
        file=sys.stderr,
    )

    load_dotenv(REPO / ".env")
    server = os.getenv("VOCECHAT_SERVER_URL")
    key = os.getenv("VOCECHAT_API_KEY")
    if not server or not key:
        print("missing VOCECHAT_SERVER_URL / VOCECHAT_API_KEY in .env", file=sys.stderr)
        return 2

    reply = Path(args.reply_file).read_text(encoding="utf-8").strip("\n")
    conv = args.conv
    try:
        resp = _send_legacy(
            server, key, conv, reply, args.markdown
        )
    except (IndexError, ValueError):
        print(f"unknown conv prefix: {conv}", file=sys.stderr)
        return 2

    if not resp.ok:
        print(f"send failed: HTTP {resp.status_code} {resp.text}", file=sys.stderr)
        return 1

    now = int(time.time() * 1000)
    record_reply({
        "conv_id": conv,
        "mid": args.mid,
        "reply": reply,
        "markdown": args.markdown,
        "bot_uid": int(os.getenv("BOT_UID", "0") or 0),
        "created_at": now,
    })

    print(f"[OK] replied to {conv} mid={args.mid}; history + state updated. server said: {resp.text[:80]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
