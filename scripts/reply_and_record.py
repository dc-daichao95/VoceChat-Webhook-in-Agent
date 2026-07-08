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
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

import send  # noqa: E402

STATE_FILE = REPO / "data" / "state.json"
INBOUND_DIR = REPO / "data" / "inbound"
HISTORY_DIR = REPO / "data" / "history"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"conversations": {}, "seen_mids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_history(conv_id: str, record: dict) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with (HISTORY_DIR / f"{conv_id}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def find_inbound_record(conv_id: str, mid: int):
    f = INBOUND_DIR / f"{conv_id}.jsonl"
    if not f.exists():
        return None
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("mid") == mid:
            return rec
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conv", required=True, help="会话 id,如 u2 / g5")
    ap.add_argument("--mid", required=True, type=int, help="要回复的入站消息 mid")
    ap.add_argument("--reply-file", required=True, help="UTF-8 回复文本文件")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()

    load_dotenv(REPO / ".env")
    server = os.getenv("VOCECHAT_SERVER_URL")
    key = os.getenv("VOCECHAT_API_KEY")
    if not server or not key:
        print("missing VOCECHAT_SERVER_URL / VOCECHAT_API_KEY in .env", file=sys.stderr)
        return 2

    reply = Path(args.reply_file).read_text(encoding="utf-8").strip("\n")
    conv = args.conv
    kind = conv[0]
    target_id = int(conv[1:])
    if kind == "u":
        resp = send.send_message(server, key, reply, uid=target_id, markdown=args.markdown)
    elif kind == "g":
        resp = send.send_message(server, key, reply, gid=target_id, markdown=args.markdown)
    else:
        print(f"unknown conv prefix: {conv}", file=sys.stderr)
        return 2

    if not resp.ok:
        print(f"send failed: HTTP {resp.status_code} {resp.text}", file=sys.stderr)
        return 1

    now = int(time.time() * 1000)
    ctype = "text/markdown" if args.markdown else "text/plain"

    # 记账:先补入站记录(若历史里还没有),再补出站记录
    in_rec = find_inbound_record(conv, args.mid)
    if in_rec is not None:
        append_history(conv, in_rec)
    append_history(conv, {
        "mid": None, "conv_id": conv, "direction": "out", "from_uid": int(os.getenv("BOT_UID", "0") or 0),
        "content_type": ctype, "content": reply, "in_reply_to": args.mid,
        "created_at": now, "recorded_at": now,
    })

    state = load_state()
    conv_state = state.setdefault("conversations", {}).setdefault(conv, {})
    conv_state["last_processed_mid"] = max(conv_state.get("last_processed_mid", -1), args.mid)
    conv_state["last_processed_at"] = now
    seen = set(state.get("seen_mids", []))
    seen.add(args.mid)
    state["seen_mids"] = sorted(seen)
    save_state(state)

    print(f"[OK] replied to {conv} mid={args.mid}; history + state updated. server said: {resp.text[:80]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
