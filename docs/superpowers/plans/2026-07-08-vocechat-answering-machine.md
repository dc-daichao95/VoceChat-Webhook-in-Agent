# VoceChat 自动应答机器人 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个 VoceChat 自动应答机器人:dumb receiver 收 webhook 并落盘到 fnOS WebDAV spool;本机 Cursor 大脑经 WebDAV 拉取、基于历史生成回复、经 bot API 发回。

**Architecture:** 接收端(FastAPI, Docker on NAS)只做过滤+落盘;本机大脑用 `/loop` 轮询 WebDAV(PROPFIND + 条件 GET)取新消息,生成回复后用 `send.py` 出站发回 VoceChat,并在本地维护权威历史与游标。核心判定逻辑抽为纯函数以便单测。

**Tech Stack:** Python 3.8+、FastAPI、uvicorn、requests、python-dotenv、pytest、responses;WebDAV over HTTPS;Docker(NAS 部署 receiver)。

**参考规格:** `docs/superpowers/specs/2026-07-08-vocechat-answering-machine-design.md`

**约定 · 消息记录 schema(每行一条 JSONL):**

```json
{"mid": 2978, "conv_id": "u7910", "direction": "in", "from_uid": 7910, "content_type": "text/markdown", "content": "...", "mentioned_bot": false, "created_at": 1672048481664, "recorded_at": 1672048482000}
```

出站记录额外含 `"in_reply_to": <mid>`,`mid` 为 `null`,`from_uid` 为 bot uid。

---

## Task 0: 项目脚手架与依赖

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `app/__init__.py`
- Create: `brain/__init__.py`
- Create: `tests/__init__.py`
- Create: `pytest.ini`

- [ ] **Step 1: 写 `requirements.txt`**

```
fastapi==0.110.0
uvicorn==0.29.0
requests==2.32.4
python-dotenv==1.0.1
pytest==8.1.1
responses==0.25.0
httpx==0.27.0
```

（`httpx` 供 FastAPI `TestClient` 使用。）

- [ ] **Step 2: 写 `.env.example`**

```
# receiver(NAS 上)
BOT_UID=0
SCOPE_DM=true
SCOPE_GROUP_MENTION=true
LISTEN_HOST=0.0.0.0
LISTEN_PORT=8091
DATA_DIR=/webhook_share
RAW_DUMP=true

# 本机 send.py
VOCECHAT_SERVER_URL=https://chat.example.com
VOCECHAT_API_KEY=replace-me
```

- [ ] **Step 3: 建空包文件**

`app/__init__.py`、`brain/__init__.py`、`tests/__init__.py` 内容均为空。

- [ ] **Step 4: 写 `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
```

- [ ] **Step 5: 安装依赖并确认 pytest 可运行**

Run: `python -m pip install -r requirements.txt && python -m pytest -q`
Expected: `no tests ran`（0 收集,退出码 5)或类似,无导入错误。

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .env.example app/__init__.py brain/__init__.py tests/__init__.py pytest.ini
git commit -m "chore: scaffold answering machine project"
```

---

## Task 1: `app/filters.py` — 纯函数(过滤 + conv_id + 记录构建)

**Files:**
- Create: `app/filters.py`
- Test: `tests/test_filters.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_filters.py
from app import filters


def _dm(mid=1, uid=7910, ctype="text/plain", typ="normal", content="hi"):
    return {"mid": mid, "from_uid": uid, "created_at": 100,
            "detail": {"type": typ, "content_type": ctype, "content": content, "properties": None},
            "target": {"uid": 999}}


def _group(mid=2, uid=7910, gid=2, mentions=None, content="hi"):
    props = {"mentions": mentions} if mentions is not None else None
    return {"mid": mid, "from_uid": uid, "created_at": 100,
            "detail": {"type": "normal", "content_type": "text/plain", "content": content, "properties": props},
            "target": {"gid": gid}}


def test_is_normal_text():
    assert filters.is_normal_text(_dm()) is True
    assert filters.is_normal_text(_dm(typ="edit")) is False
    assert filters.is_normal_text(_dm(ctype="vocechat/file")) is False


def test_conv_id_of():
    assert filters.conv_id_of(_dm(uid=7910)) == "u7910"
    assert filters.conv_id_of(_group(gid=2)) == "g2"
    assert filters.conv_id_of({"target": {}}) is None


def test_should_accept_dm():
    assert filters.should_accept(_dm(), bot_uid=0, scope_dm=True, scope_group_mention=True) is True
    assert filters.should_accept(_dm(), bot_uid=0, scope_dm=False, scope_group_mention=True) is False


def test_should_accept_own_message_rejected():
    assert filters.should_accept(_dm(uid=0), bot_uid=0, scope_dm=True, scope_group_mention=True) is False


def test_should_accept_group_requires_mention():
    assert filters.should_accept(_group(mentions=[0]), bot_uid=0, scope_dm=True, scope_group_mention=True) is True
    assert filters.should_accept(_group(mentions=[5]), bot_uid=0, scope_dm=True, scope_group_mention=True) is False
    assert filters.should_accept(_group(mentions=None), bot_uid=0, scope_dm=True, scope_group_mention=True) is False


def test_build_in_record():
    rec = filters.build_in_record(_group(mid=2, uid=7910, gid=2, mentions=[0]), bot_uid=0)
    assert rec["mid"] == 2 and rec["conv_id"] == "g2" and rec["direction"] == "in"
    assert rec["mentioned_bot"] is True and rec["content"] == "hi"
    assert isinstance(rec["recorded_at"], int)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_filters.py -q`
Expected: FAIL(`ModuleNotFoundError: No module named 'app.filters'`)

- [ ] **Step 3: 写实现**

```python
# app/filters.py
from __future__ import annotations

import time

TEXT_TYPES = {"text/plain", "text/markdown"}


def is_normal_text(payload: dict) -> bool:
    detail = payload.get("detail") or {}
    return detail.get("type") == "normal" and detail.get("content_type") in TEXT_TYPES


def conv_id_of(payload: dict):
    target = payload.get("target") or {}
    if "uid" in target:
        return f"u{payload.get('from_uid')}"
    if "gid" in target:
        return f"g{target['gid']}"
    return None


def mentioned_uids(payload: dict) -> list:
    detail = payload.get("detail") or {}
    props = detail.get("properties")
    if isinstance(props, dict) and isinstance(props.get("mentions"), list):
        out = []
        for u in props["mentions"]:
            try:
                out.append(int(u))
            except (TypeError, ValueError):
                continue
        return out
    return []


def should_accept(payload: dict, *, bot_uid: int, scope_dm: bool, scope_group_mention: bool) -> bool:
    if not is_normal_text(payload):
        return False
    if payload.get("from_uid") == bot_uid:
        return False
    target = payload.get("target") or {}
    if "uid" in target:
        return scope_dm
    if "gid" in target:
        return scope_group_mention and bot_uid in mentioned_uids(payload)
    return False


def build_in_record(payload: dict, bot_uid: int) -> dict:
    detail = payload.get("detail") or {}
    return {
        "mid": payload.get("mid"),
        "conv_id": conv_id_of(payload),
        "direction": "in",
        "from_uid": payload.get("from_uid"),
        "content_type": detail.get("content_type"),
        "content": detail.get("content", ""),
        "mentioned_bot": bot_uid in mentioned_uids(payload),
        "created_at": payload.get("created_at"),
        "recorded_at": int(time.time() * 1000),
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_filters.py -q`
Expected: PASS(6 passed)

> **注（Phase 0 验证）**:`mentioned_uids` 假定 `detail.properties.mentions` 为 uid 列表。Phase 0 抓到真实 payload 后,若结构不同(见 Task 10),需回来修正 `mentioned_uids` 并补一条对应测试。

- [ ] **Step 5: Commit**

```bash
git add app/filters.py tests/test_filters.py
git commit -m "feat: add pure webhook filtering + conv_id + record builder"
```

---

## Task 2: `app/storage.py` — 落盘(JSONL 追加 / seen_mids 去重 / raw dump)

**Files:**
- Create: `app/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_storage.py -q`
Expected: FAIL(`No module named 'app.storage'`)

- [ ] **Step 3: 写实现**

```python
# app/storage.py
from __future__ import annotations

import json
import time
from pathlib import Path


def _conv_path(data_dir: str, conv_id: str) -> Path:
    return Path(data_dir) / "conversations" / f"{conv_id}.jsonl"


def append_message(data_dir: str, conv_id: str, record: dict) -> None:
    p = _conv_path(data_dir, conv_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _seen_path(data_dir: str) -> Path:
    return Path(data_dir) / "seen_mids.json"


def load_seen_mids(data_dir: str) -> set:
    p = _seen_path(data_dir)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, ValueError):
        return set()


def save_seen_mids(data_dir: str, mids: set) -> None:
    p = _seen_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(m for m in mids if m is not None)), encoding="utf-8")


def dump_raw(data_dir: str, mid, payload: dict) -> None:
    d = Path(data_dir) / "raw"
    d.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    (d / f"{ts}_{mid}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_storage.py -q`
Expected: PASS(4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/storage.py tests/test_storage.py
git commit -m "feat: add spool storage (jsonl append, seen_mids, raw dump)"
```

---

## Task 3: `app/config.py` — receiver 配置加载与校验

**Files:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py
import pytest

from app.config import load_config


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("BOT_UID", "123")
    monkeypatch.setenv("SCOPE_DM", "false")
    monkeypatch.setenv("RAW_DUMP", "true")
    monkeypatch.setenv("DATA_DIR", "/tmp/x")
    cfg = load_config(env_path=None)
    assert cfg.bot_uid == 123
    assert cfg.scope_dm is False
    assert cfg.scope_group_mention is True  # default
    assert cfg.raw_dump is True
    assert cfg.data_dir == "/tmp/x"


def test_load_config_missing_bot_uid(monkeypatch):
    monkeypatch.delenv("BOT_UID", raising=False)
    with pytest.raises(ValueError):
        load_config(env_path=None)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL(`No module named 'app.config'`)

- [ ] **Step 3: 写实现**

```python
# app/config.py
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Config:
    bot_uid: int
    scope_dm: bool
    scope_group_mention: bool
    data_dir: str
    raw_dump: bool
    listen_host: str
    listen_port: int


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(env_path=None) -> Config:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()
    if not os.environ.get("BOT_UID"):
        raise ValueError("BOT_UID is required in environment")
    return Config(
        bot_uid=int(os.environ["BOT_UID"]),
        scope_dm=_as_bool(os.getenv("SCOPE_DM"), True),
        scope_group_mention=_as_bool(os.getenv("SCOPE_GROUP_MENTION"), True),
        data_dir=os.getenv("DATA_DIR", "./server_data"),
        raw_dump=_as_bool(os.getenv("RAW_DUMP"), False),
        listen_host=os.getenv("LISTEN_HOST", "0.0.0.0"),
        listen_port=int(os.getenv("LISTEN_PORT", "8091")),
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS(2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: add receiver config loading with validation"
```

---

## Task 4: `app/receiver.py` — FastAPI 端点(串联 filters + storage)

**Files:**
- Create: `app/receiver.py`
- Test: `tests/test_receiver.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_receiver.py
from fastapi.testclient import TestClient

from app.config import Config
from app.receiver import create_app


def _cfg(tmp_path, raw_dump=False):
    return Config(bot_uid=0, scope_dm=True, scope_group_mention=True,
                  data_dir=str(tmp_path), raw_dump=raw_dump,
                  listen_host="0.0.0.0", listen_port=8091)


def _dm(mid=1, uid=7910, content="hi"):
    return {"mid": mid, "from_uid": uid, "created_at": 100,
            "detail": {"type": "normal", "content_type": "text/plain", "content": content, "properties": None},
            "target": {"uid": 999}}


def test_probe_and_health(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    assert c.get("/").text == "ok"
    assert c.get("/health").json() == {"status": "healthy"}


def test_post_accepts_and_persists_dm(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    r = c.post("/", json=_dm(mid=5))
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    p = tmp_path / "conversations" / "u7910.jsonl"
    assert p.exists() and '"mid": 5' in p.read_text(encoding="utf-8")


def test_post_ignores_own_message(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    c.post("/", json=_dm(mid=6, uid=0))
    assert not (tmp_path / "conversations").exists()


def test_post_dedup_same_mid(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    c.post("/", json=_dm(mid=7))
    c.post("/", json=_dm(mid=7))
    lines = (tmp_path / "conversations" / "u7910.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_post_bad_json_returns_200(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    r = c.post("/", data="not-json", headers={"content-type": "application/json"})
    assert r.status_code == 200


def test_raw_dump_writes_even_non_normal(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path, raw_dump=True)))
    payload = _dm(mid=8); payload["detail"]["type"] = "edit"
    c.post("/", json=payload)
    assert list((tmp_path / "raw").glob("*_8.json"))
    assert not (tmp_path / "conversations").exists()  # edit 不落 conversations
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_receiver.py -q`
Expected: FAIL(`No module named 'app.receiver'`)

- [ ] **Step 3: 写实现**

```python
# app/receiver.py
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import filters, storage
from .config import Config

log = logging.getLogger("receiver")


def create_app(config: Config) -> FastAPI:
    app = FastAPI()

    @app.get("/", response_class=PlainTextResponse)
    def probe() -> str:
        return "ok"

    @app.get("/health")
    def health() -> dict:
        return {"status": "healthy"}

    @app.post("/")
    async def receive(request: Request):
        try:
            payload = await request.json()
        except Exception:
            log.warning("received non-JSON body; ignoring")
            return JSONResponse({"status": "ok"})
        try:
            _process(config, payload)
        except Exception:
            log.exception("error while processing webhook payload")
        return JSONResponse({"status": "ok"})

    return app


def _process(config: Config, payload: dict) -> None:
    if config.raw_dump:
        storage.dump_raw(config.data_dir, payload.get("mid"), payload)
    if not filters.should_accept(
        payload,
        bot_uid=config.bot_uid,
        scope_dm=config.scope_dm,
        scope_group_mention=config.scope_group_mention,
    ):
        return
    mid = payload.get("mid")
    seen = storage.load_seen_mids(config.data_dir)
    if mid in seen:
        return
    conv_id = filters.conv_id_of(payload)
    storage.append_message(config.data_dir, conv_id, filters.build_in_record(payload, config.bot_uid))
    seen.add(mid)
    storage.save_seen_mids(config.data_dir, seen)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_receiver.py -q`
Expected: PASS(6 passed)

- [ ] **Step 5: Commit**

```bash
git add app/receiver.py tests/test_receiver.py
git commit -m "feat: add dumb webhook receiver (FastAPI endpoints)"
```

---

## Task 5: `send.py` — 出站发送 CLI

**Files:**
- Create: `send.py`
- Test: `tests/test_send.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_send.py
import responses

import send


def test_build_url_uid():
    assert send.build_url("https://chat.example.com/", uid=1) == "https://chat.example.com/api/bot/send_to_user/1"


def test_build_url_gid():
    assert send.build_url("https://chat.example.com", gid=2) == "https://chat.example.com/api/bot/send_to_group/2"


@responses.activate
def test_send_message_uid_text():
    responses.add(responses.POST, "https://chat.example.com/api/bot/send_to_user/1", body="ok", status=200)
    r = send.send_message("https://chat.example.com", "KEY", "hello", uid=1)
    assert r.status_code == 200
    sent = responses.calls[0].request
    assert sent.headers["x-api-key"] == "KEY"
    assert sent.headers["content-type"] == "text/plain"
    assert sent.body == b"hello"


@responses.activate
def test_send_message_gid_markdown():
    responses.add(responses.POST, "https://chat.example.com/api/bot/send_to_group/2", body="ok", status=200)
    send.send_message("https://chat.example.com", "KEY", "**hi**", gid=2, markdown=True)
    assert responses.calls[0].request.headers["content-type"] == "text/markdown"


@responses.activate
def test_main_missing_env_returns_2(monkeypatch):
    monkeypatch.delenv("VOCECHAT_SERVER_URL", raising=False)
    monkeypatch.delenv("VOCECHAT_API_KEY", raising=False)
    assert send.main(["--target-uid", "1", "--text", "hi"]) == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_send.py -q`
Expected: FAIL(`No module named 'send'`)

- [ ] **Step 3: 写实现**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_send.py -q`
Expected: PASS(5 passed)

- [ ] **Step 5: Commit**

```bash
git add send.py tests/test_send.py
git commit -m "feat: add outbound send.py CLI"
```

---

## Task 6: `brain/select.py` — 从 inbound+state 选待处理(纯函数)

**Files:**
- Create: `brain/select.py`
- Test: `tests/test_select.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_select.py
from brain import select


def _rec(mid, direction="in"):
    return {"mid": mid, "conv_id": "u1", "direction": direction}


def test_select_skips_out_and_processed():
    records = [_rec(1), _rec(2, "out"), _rec(3), _rec(4)]
    out = select.select_pending("u1", records, last_processed_mid=2, seen_mids=set())
    assert [r["mid"] for r in out] == [3, 4]


def test_select_skips_seen_and_none_mid():
    records = [_rec(3), _rec(None), _rec(5)]
    out = select.select_pending("u1", records, last_processed_mid=0, seen_mids={3})
    assert [r["mid"] for r in out] == [5]


def test_select_sorted_ascending():
    records = [_rec(9), _rec(7), _rec(8)]
    out = select.select_pending("u1", records, last_processed_mid=0, seen_mids=set())
    assert [r["mid"] for r in out] == [7, 8, 9]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_select.py -q`
Expected: FAIL(`No module named 'brain.select'`)

- [ ] **Step 3: 写实现**

```python
# brain/select.py
from __future__ import annotations


def select_pending(conv_id: str, records: list, last_processed_mid: int, seen_mids: set) -> list:
    pending = []
    for r in records:
        if r.get("direction") != "in":
            continue
        mid = r.get("mid")
        if mid is None or mid <= last_processed_mid or mid in seen_mids:
            continue
        pending.append(r)
    return sorted(pending, key=lambda r: r["mid"])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_select.py -q`
Expected: PASS(3 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/select.py tests/test_select.py
git commit -m "feat: add pending-message selection (pure)"
```

---

## Task 7: `brain/pull.py` — WebDAV 拉取(PROPFIND 列 + 条件 GET)

**Files:**
- Create: `brain/pull.py`
- Test: `tests/test_pull.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pull.py
import responses

from brain import pull

PROPFIND_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response><d:href>/webhook_share/conversations/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
  <d:response><d:href>/webhook_share/conversations/u7910.jsonl</d:href>
    <d:propstat><d:prop><d:getetag>"aaa"</d:getetag><d:resourcetype/></d:prop></d:propstat></d:response>
</d:multistatus>"""


def test_parse_listing_extracts_files():
    entries = pull.parse_listing(PROPFIND_XML)
    files = [e for e in entries if not e["is_dir"]]
    assert len(files) == 1
    assert files[0]["name"] == "u7910.jsonl"
    assert files[0]["etag"] == '"aaa"'


@responses.activate
def test_pull_downloads_changed_and_skips_unchanged(tmp_path):
    base = "https://nas.example.com/webhook_share/"
    responses.add("PROPFIND", "https://nas.example.com/webhook_share/conversations/", body=PROPFIND_XML, status=207)
    responses.add(responses.GET, "https://nas.example.com/webhook_share/conversations/u7910.jsonl",
                  body='{"mid": 1}\n', status=200, headers={"ETag": '"aaa"'})

    client = pull.WebDAVClient(base, "u", "p", verify=False)
    state = {"conversations": {}}
    new_state = pull.pull_conversations(client, "conversations/", state, str(tmp_path))

    assert (tmp_path / "u7910.jsonl").read_text(encoding="utf-8") == '{"mid": 1}\n'
    assert new_state["conversations"]["u7910"]["etag"] == '"aaa"'

    # 第二轮:etag 未变 → 不应再产生 GET(仅 PROPFIND)
    calls_before = len(responses.calls)
    pull.pull_conversations(client, "conversations/", new_state, str(tmp_path))
    get_calls = [c for c in responses.calls[calls_before:] if c.request.method == "GET"]
    assert get_calls == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_pull.py -q`
Expected: FAIL(`No module named 'brain.pull'`)

- [ ] **Step 3: 写实现**

```python
# brain/pull.py
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
import urllib3
from requests.auth import HTTPBasicAuth

DAV = "{DAV:}"


class WebDAVClient:
    def __init__(self, base_url: str, user: str, passwd: str, verify: bool = False):
        self.base = base_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, passwd)
        self.session.verify = verify
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _url(self, path: str) -> str:
        return urljoin(self.base, path.lstrip("/"))

    def list_dir(self, path: str, timeout: float = 15) -> list:
        r = self.session.request("PROPFIND", self._url(path), headers={"Depth": "1"}, timeout=timeout)
        r.raise_for_status()
        return parse_listing(r.text)

    def get(self, path: str, etag=None, timeout: float = 30) -> requests.Response:
        headers = {"If-None-Match": etag} if etag else {}
        return self.session.get(self._url(path), headers=headers, timeout=timeout)


def parse_listing(xml_text: str) -> list:
    entries = []
    root = ET.fromstring(xml_text)
    for resp in root.findall(f"{DAV}response"):
        href_el = resp.find(f"{DAV}href")
        if href_el is None or not href_el.text:
            continue
        href = unquote(href_el.text)
        prop = resp.find(f"{DAV}propstat/{DAV}prop")
        is_dir = False
        etag = ""
        if prop is not None:
            rtype = prop.find(f"{DAV}resourcetype")
            is_dir = rtype is not None and rtype.find(f"{DAV}collection") is not None
            et = prop.find(f"{DAV}getetag")
            if et is not None and et.text:
                etag = et.text
        name = href.rstrip("/").split("/")[-1]
        entries.append({"href": href, "name": name, "is_dir": is_dir, "etag": etag})
    return entries


def pull_conversations(client: WebDAVClient, remote_dir: str, state: dict, inbound_dir: str) -> dict:
    """列出 remote_dir 下的 *.jsonl,对 etag 变化的文件下载到 inbound_dir,并更新 state。"""
    convs = state.setdefault("conversations", {})
    Path(inbound_dir).mkdir(parents=True, exist_ok=True)
    for entry in client.list_dir(remote_dir):
        if entry["is_dir"] or not entry["name"].endswith(".jsonl"):
            continue
        conv_id = entry["name"][:-len(".jsonl")]
        known_etag = convs.get(conv_id, {}).get("etag")
        if entry["etag"] and entry["etag"] == known_etag:
            continue  # 未变,跳过下载
        resp = client.get(remote_dir.rstrip("/") + "/" + entry["name"], etag=known_etag)
        if resp.status_code == 304:
            continue
        resp.raise_for_status()
        (Path(inbound_dir) / entry["name"]).write_bytes(resp.content)
        convs.setdefault(conv_id, {})["etag"] = resp.headers.get("ETag", entry["etag"])
    return state
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_pull.py -q`
Expected: PASS(2 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/pull.py tests/test_pull.py
git commit -m "feat: add WebDAV pull with conditional GET"
```

---

## Task 8: 部署与运行支撑(Dockerfile / 启动脚本 / loop 手册)

**Files:**
- Create: `Dockerfile`
- Create: `scripts/run_receiver.sh`
- Create: `scripts/loop_prompt.md`

- [ ] **Step 1: 写 `Dockerfile`(receiver on NAS)**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DATA_DIR=/webhook_share LISTEN_HOST=0.0.0.0 LISTEN_PORT=8091
EXPOSE 8091
CMD ["uvicorn", "app.receiver:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091"]
```

- [ ] **Step 2: 写 `scripts/run_receiver.sh`**

```bash
#!/usr/bin/env bash
# 在 fnOS NAS 上构建并运行 receiver 容器;把 WebDAV 暴露的目录挂进容器 /webhook_share
set -euo pipefail
IMAGE=answeringmachine-receiver
docker build -t "$IMAGE" .
docker run -d --name "$IMAGE" --restart unless-stopped \
  -p 8091:8091 \
  -e BOT_UID="${BOT_UID:?set BOT_UID}" \
  -e SCOPE_DM=true -e SCOPE_GROUP_MENTION=true -e RAW_DUMP=true \
  -v /vol1/webhook_share:/webhook_share \
  "$IMAGE"
```

（`-v` 左侧改成 NAS 上被 WebDAV 暴露为 `/webhook_share` 的真实宿主路径。)

- [ ] **Step 3: 写 `scripts/loop_prompt.md`(大脑每轮操作手册,供 `/loop` 使用)**

```markdown
# AnsweringMachine 大脑轮询手册

每轮执行:

1. 拉取:运行
   `python -c "from brain import pull; import json; ..."`
   —— 用 `share.env`(url/user/passwd)构造 `pull.WebDAVClient`,
   调用 `pull.pull_conversations(client, "conversations/", state, "data/inbound")`,
   其中 `state` 从 `data/state.json` 读取(不存在则 `{"conversations":{}}`),完成后写回。
2. 扫描:对 `data/inbound/<conv_id>.jsonl` 逐文件读取记录,用
   `brain.select.select_pending(conv_id, records, last_processed_mid, seen_mids)`
   选出待处理入站消息(`last_processed_mid`、`seen_mids` 取自 `data/state.json`)。
3. 逐条(按 conv_id、mid 升序)处理:
   a. 读 `data/history/<conv_id>.jsonl` 作为上下文(全量)。
   b. 由你(大脑)基于历史 + 本条消息生成回复文本。
   c. 把该入站记录追加进 `data/history/<conv_id>.jsonl`(direction=in)。
   d. 发送:私聊 `python send.py --target-uid <uid> --text -`(经 stdin 传文本);
      群聊 `python send.py --target-gid <gid> --text -`;需要 markdown 加 `--markdown`。
   e. 发送成功(退出码 0):把出站记录追加进 history(direction=out, in_reply_to=<mid>),
      更新 `data/state.json`:`conversations[conv_id].last_processed_mid=<mid>`、`seen_mids += <mid>`。
   f. 发送失败:记日志,不推进游标(下轮重试);连续失败超过 3 次则跳过该 mid 并告警。
4. 无新消息则本轮结束。

注意:任何一步异常都只跳过当前条目,不中断整轮;游标只在"发送成功"后推进。
```

- [ ] **Step 4: 冒烟验证 Docker 构建(可选,需本地 docker)**

Run: `docker build -t answeringmachine-receiver .`
Expected: 构建成功;无 docker 环境则跳过并在 commit message 注明未验证。

- [ ] **Step 5: Commit**

```bash
git add Dockerfile scripts/run_receiver.sh scripts/loop_prompt.md
git commit -m "chore: add receiver Dockerfile, run script, and loop prompt"
```

---

## Task 9: `README.md` — 安装、部署与运行说明

**Files:**
- Create: `README.md`

- [ ] **Step 1: 写 `README.md`**

````markdown
# AnsweringMachine

VoceChat 自动应答机器人:dumb receiver(FastAPI, 部署在 fnOS NAS 的 Docker)收 webhook 并落盘到 WebDAV spool;本机 Cursor 会话作为"大脑",经 WebDAV 拉取新消息、基于历史生成回复,再用 `send.py` 经 bot API 发回。

设计文档见 `docs/superpowers/specs/2026-07-08-vocechat-answering-machine-design.md`。

## 安装

```bash
python -m pip install -r requirements.txt
```

## 配置

- receiver:复制 `.env.example` 为 `.env`,填 `BOT_UID` 等。
- 本机发送:同 `.env` 填 `VOCECHAT_SERVER_URL` / `VOCECHAT_API_KEY`。
- 本机拉取:`share.env` 填 `url` / `user` / `passwd`(fnOS WebDAV)。

`.env` 与 `share.env` 均不进 git。

## 部署 receiver(NAS)

```bash
BOT_UID=<bot uid> bash scripts/run_receiver.sh
```

在 VoceChat 的 bot 设置里把 webhook URL 指向 `http(s)://<nas>:8091/`。

## 运行大脑(本机)

在 Cursor 会话里用 `/loop`(30~60s)执行 `scripts/loop_prompt.md`。

## 连通性自检

```bash
python scripts/webdav_check.py --roundtrip   # PUT->GET->DELETE 往返
python scripts/webdav_check.py --bench 20    # 轮询成本
```

## 测试

```bash
python -m pytest -q
```
````

- [ ] **Step 2: 运行全部测试确认绿**

Run: `python -m pytest -q`
Expected: PASS(全部通过)

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add project README"
```

---

## Task 10: Phase 0/2/3 联调验收(手动冒烟)

> 这些是手动集成验收,不进单测。前置:receiver 已部署、WebDAV 凭证可用(已验证)、bot API key 就绪。

- [ ] **Step 1: Phase 0 — 测通 webhook + 出站**

在 NAS 用 `RAW_DUMP=true` 起 receiver;从 VoceChat 给 bot 发一条私聊。
验收:`/webhook_share/raw/` 出现该 payload;打开确认 `detail.properties`(@提及)、`target`、`content_type` 的真实结构。
本机 `python send.py --target-uid <你的uid> --text "hi from bot"` → VoceChat 收到回复。

- [ ] **Step 2: 若 `properties` 结构与假设不符,修正 mention 解析**

依据 Step 1 抓到的真实结构,更新 `app/filters.py` 的 `mentioned_uids`,并在 `tests/test_filters.py` 增补一条按真实结构构造的用例;`python -m pytest tests/test_filters.py -q` 通过后提交:
```bash
git add app/filters.py tests/test_filters.py
git commit -m "fix: align mention parsing with real VoceChat properties"
```

- [ ] **Step 3: Phase 2 — 端到端私聊自动应答**

在 Cursor 会话启动 `/loop` 执行 `scripts/loop_prompt.md`;私聊 bot 一条消息。
验收:数十秒内收到基于历史的回复;`data/state.json` 的 `last_processed_mid`/`etag` 更新;`data/history/<conv_id>.jsonl` 同时含 in 与 out;不出现自我循环。

- [ ] **Step 4: Phase 3 — 群 @ 应答**

把 bot 拉进一个群;群里 @ bot 发消息、再发一条不 @ 的。
验收:@ 的被应答、不 @ 的被忽略。

- [ ] **Step 5: 关闭 Phase 0 调试项**

联调稳定后把 receiver 的 `RAW_DUMP` 设为 `false` 并重启容器,减少无谓写盘。

---

## Self-Review(计划自审结果)

- **Spec 覆盖**:§2 组件 → Task 1/2/4;§3 连通性(WebDAV 条件 GET)→ Task 7;§5 数据模型(记录/seen_mids/state.etag)→ Task 1/2/6/7;§6 receiver → Task 4;§7 大脑循环 + send.py → Task 5/6/8(loop_prompt);§8 配置 → Task 3 + .env.example + README;§9 错误处理 → Task 4(坏 JSON/异常返回 200)、Task 7(raise_for_status/304)、loop_prompt(游标不推进);§10 测试与里程碑 → 各 Task 单测 + Task 10 手动;§11 项目结构 → 全体;§12/§13(选型/成本)→ 已在 spec 固化,`webdav_check.py` 已存在。
- **占位符**:无 TBD;mention 解析给出了具体实现 + Phase 0 验证/修正任务(Task 10 Step 2)。
- **类型一致性**:`Config` 字段、`filters.*` 签名、`storage.*` 路径、`select_pending(conv_id, records, last_processed_mid, seen_mids)`、`WebDAVClient`/`parse_listing`/`pull_conversations` 在各任务与测试中命名一致;`state` 结构统一为 `{"conversations": {conv_id: {last_processed_mid, last_processed_at, etag}}, "seen_mids": [...]}`。
