#!/usr/bin/env python3
"""本机大脑单轮运行器(默认 dry-run,只拉取+列出待处理,不发送)。

流程:
  1. 从 share.env 读 WebDAV 凭据,构造客户端。
  2. pull.pull_conversations 把 conversations/*.jsonl 条件下载到 data/inbound/。
  3. 对每个会话用 select.select_pending 选出待处理入站消息并打印预览。

用法:
  python scripts/brain_cycle.py            # dry-run:拉取 + 列出待处理
  python scripts/brain_cycle.py --explore  # 额外打印 WebDAV 目录结构(排障用)

发送回复不在本脚本内自动进行 —— 由大脑(Cursor)读取待处理内容后,
用 send.py 发出,再更新 data/state.json(见 skill/loop_prompt.md)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain import pull, select  # noqa: E402

SHARE_ENV = REPO / "share.env"
STATE_FILE = REPO / "data" / "state.json"
INBOUND_DIR = REPO / "data" / "inbound"
REMOTE_DIR = "conversations/"


def load_share_env() -> dict:
    cfg = {}
    for line in SHARE_ENV.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"conversations": {}, "seen_mids": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def explore(client: pull.WebDAVClient, path: str) -> None:
    print(f"--- LISTING {path} ---")
    try:
        for e in client.list_dir(path):
            print(f"  {'DIR ' if e['is_dir'] else 'FILE'} {e['name']!r} etag={e['etag']!r}")
    except Exception as ex:  # noqa: BLE001
        print(f"  (error: {ex})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--explore", action="store_true", help="打印 WebDAV 目录结构")
    args = ap.parse_args()

    cfg = load_share_env()
    client = pull.WebDAVClient(cfg["url"], cfg["user"], cfg["passwd"], verify=False)

    if args.explore:
        explore(client, "/")
        explore(client, REMOTE_DIR)
        print()

    state = load_state()
    try:
        pull.pull_conversations(client, REMOTE_DIR, state, str(INBOUND_DIR))
    except Exception as ex:  # noqa: BLE001
        print(f"[pull] conversations/ 拉取失败(可能还没有该目录/新消息): {ex}")
        print("       —— 让用户在 VoceChat 给 bot 发一条消息后再试。")
        save_state(state)
        return 0
    save_state(state)

    seen = set(state.get("seen_mids", []))
    total_pending = 0
    for f in sorted(INBOUND_DIR.glob("*.jsonl")):
        conv_id = f.stem
        records = []
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        last = state.get("conversations", {}).get(conv_id, {}).get("last_processed_mid", -1)
        pending = select.select_pending(conv_id, records, last, seen)
        if pending:
            total_pending += len(pending)
            print(f"[{conv_id}] {len(pending)} 条待处理:")
            for r in pending:
                preview = (r.get("content", "") or "")[:60]
                print(f"   mid={r['mid']} from_uid={r.get('from_uid')} : {preview!r}")
    if total_pending == 0:
        print("没有待处理消息。")
    else:
        print(f"\n共 {total_pending} 条待处理。大脑读取内容后用 send.py 回复,再更新 state。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
