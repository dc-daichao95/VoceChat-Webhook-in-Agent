# send.py
from __future__ import annotations

import argparse
import os
import sys

import requests
from dotenv import load_dotenv


def build_url(server_url: str, *, uid=None, gid=None) -> str:
    base = server_url.rstrip("/")
    if uid is not None:
        return f"{base}/api/bot/send_to_user/{uid}"
    if gid is not None:
        return f"{base}/api/bot/send_to_group/{gid}"
    raise ValueError("need uid or gid")


def send_message(server_url: str, api_key: str, text: str, *, uid=None, gid=None, markdown=False, timeout=30) -> requests.Response:
    url = build_url(server_url, uid=uid, gid=gid)
    ctype = "text/markdown" if markdown else "text/plain"
    return requests.post(url, data=text.encode("utf-8"),
                         headers={"x-api-key": api_key, "content-type": ctype}, timeout=timeout)


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Send a message to VoceChat via bot API")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--target-uid", type=int)
    group.add_argument("--target-gid", type=int)
    ap.add_argument("--text", required=True, help="message text, or '-' to read from stdin")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)

    text = sys.stdin.read() if args.text == "-" else args.text
    server = os.getenv("VOCECHAT_SERVER_URL")
    key = os.getenv("VOCECHAT_API_KEY")
    if not server or not key:
        print("missing VOCECHAT_SERVER_URL / VOCECHAT_API_KEY", file=sys.stderr)
        return 2
    r = send_message(server, key, text, uid=args.target_uid, gid=args.target_gid, markdown=args.markdown)
    if r.ok:
        print(r.text)
        return 0
    print(f"send failed: HTTP {r.status_code} {r.text}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
