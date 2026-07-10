# 可靠调度器与联网消息响应优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将固定 `/loop` 改为独立可靠调度器 + Cursor 队列消费者，并通过 HTTP 快路径、10 秒确认、45 秒部分结果/状态消除联网消息长时间无响应。

**Architecture:** 常驻调度器负责 WebDAV 拉取、SQLite 幂等入队、SLA 通知、退避与恢复；Cursor 在线消费者按会话领取任务、生成正式回复。联网任务优先直接 HTTP，并把证据逐步写入队列，仅在必须交互时使用 browser-use。

**Tech Stack:** Python 3.8+、标准库 `sqlite3`/`asyncio`、`requests`、现有 WebDAV/VoceChat 客户端、pytest、Windows PowerShell/任务计划程序。

**Design:** `docs/superpowers/specs/2026-07-10-reliable-scheduler-online-response-design.md`

---

## 文件结构

新增：

- `scheduler/__init__.py`
- `scheduler/db.py`：SQLite schema、事务、幂等入队、租约和状态转换。
- `scheduler/policy.py`：轮询间隔、重试退避和 SLA 动作纯函数。
- `scheduler/ingest.py`：复用 WebDAV 拉取并将新入站消息入队。
- `scheduler/notifier.py`：占位/部分结果发送，不推进正式回复游标。
- `scheduler/service.py`：常驻调度循环。
- `scheduler/online.py`：HTTP 快路径与结构化证据。
- `scripts/scheduler.py`：调度器 CLI 入口。
- `scripts/queue_cli.py`：Cursor 领取、续租、写证据、完成/失败任务。
- `scripts/online_fetch.py`：Cursor 可调用的快速 HTTP 工具。
- `scripts/scheduler_install.ps1`
- `scripts/scheduler_start.ps1`
- `scripts/scheduler_stop.ps1`
- `scripts/scheduler_status.ps1`
- `scripts/scheduler_uninstall.ps1`
- `skill/queue_consumer.md`
- 对应 `tests/test_scheduler_*.py`。

修改：

- `.env.example`：调度器参数。
- `.gitignore`：忽略 SQLite WAL/日志。
- `skill/loop_prompt.md`：从固定轮询改为队列消费者。
- `README.md`、`docs/TODO.md`。

---

### Task 1: SQLite 队列与幂等状态机

**Files:**
- Create: `scheduler/__init__.py`
- Create: `scheduler/db.py`
- Create: `tests/test_scheduler_db.py`

- [ ] **Step 1: Write failing queue tests**

```python
# tests/test_scheduler_db.py
import json

from scheduler.db import QueueDB


def payload(mid=1431, conv_id="u2"):
    return {"mid": mid, "conv_id": conv_id, "direction": "in", "content": "查天气"}


def test_enqueue_is_idempotent(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    first = db.enqueue(payload(), detected_at=1000)
    second = db.enqueue(payload(), detected_at=2000)
    assert first == second
    assert db.get(first)["detected_at"] == 1000


def test_claim_is_fifo_per_conversation(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    first = db.enqueue(payload(10), detected_at=1000)
    db.enqueue(payload(11), detected_at=1001)
    jobs = db.claim(owner="cursor-a", now=1010, limit=3, lease_seconds=60)
    assert [j["id"] for j in jobs] == [first]


def test_expired_lease_returns_to_retry(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)
    db.claim(owner="cursor-a", now=1010, limit=1, lease_seconds=10)
    assert db.recover_expired(now=1021) == 1
    assert db.get(job_id)["status"] == "retry_wait"


def test_reply_markers_are_idempotent(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(payload(), detected_at=1000)
    assert db.mark_ack_sent(job_id, sent_at=1010) is True
    assert db.mark_ack_sent(job_id, sent_at=1011) is False
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -q tests/test_scheduler_db.py`

Expected: import failure for `scheduler.db`.

- [ ] **Step 3: Implement schema and QueueDB**

Use this schema in `scheduler/db.py`:

```python
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL,
    mid INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processing','retry_wait','done','cancelled')),
    network_mode TEXT NOT NULL DEFAULT 'unknown'
        CHECK(network_mode IN ('unknown','none','fast_http','browser')),
    detected_at INTEGER NOT NULL,
    available_at INTEGER NOT NULL,
    lease_owner TEXT,
    lease_until INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    ack_sent_at INTEGER,
    partial_sent_at INTEGER,
    final_sent_at INTEGER,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(conv_id, mid)
);
CREATE INDEX IF NOT EXISTS idx_jobs_available
ON jobs(status, available_at, detected_at);
"""
```

Implement:

```python
class QueueDB:
    def __init__(self, path): ...
    def enqueue(self, payload: dict, detected_at: int) -> int: ...
    def get(self, job_id: int) -> dict: ...
    def claim(self, owner: str, now: int, limit: int, lease_seconds: int) -> list: ...
    def renew(self, job_id: int, owner: str, now: int, lease_seconds: int) -> bool: ...
    def recover_expired(self, now: int) -> int: ...
    def mark_ack_sent(self, job_id: int, sent_at: int) -> bool: ...
    def mark_partial_sent(self, job_id: int, sent_at: int) -> bool: ...
    def append_evidence(self, job_id: int, evidence: dict, now: int) -> None: ...
    def complete(self, job_id: int, owner: str, sent_at: int) -> bool: ...
    def fail(self, job_id: int, owner: str, error: str, available_at: int) -> bool: ...
    def cancel(self, job_id: int, now: int) -> bool: ...
    def due_for_ack(self, now: int, delay_ms: int) -> list: ...
    def due_for_partial(self, now: int, delay_ms: int) -> list: ...
```

Implementation constraints:

- Open a fresh SQLite connection per method with `timeout=10`.
- Use `BEGIN IMMEDIATE` for claim/state transitions.
- `enqueue` uses `INSERT ... ON CONFLICT(conv_id,mid) DO NOTHING`, then selects the ID.
- `claim` selects only the earliest available unfinished job per conversation and limits distinct conversations to 3.
- Return rows as dictionaries; decode `payload_json` and `evidence_json`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest -q tests/test_scheduler_db.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add scheduler/__init__.py scheduler/db.py tests/test_scheduler_db.py
git commit -m "feat: add durable SQLite job queue with leases and idempotency"
```

---

### Task 2: Timing, quiet-hours, retry and SLA policies

**Files:**
- Create: `scheduler/policy.py`
- Create: `tests/test_scheduler_policy.py`

- [ ] **Step 1: Write failing policy tests**

```python
from datetime import datetime

from scheduler.policy import next_poll_seconds, retry_delay_seconds, sla_action


def test_poll_interval_active_normal_idle_and_quiet():
    assert next_poll_seconds(idle_rounds=0, had_message=True, now=datetime(2026, 7, 10, 9)) == 15
    assert next_poll_seconds(idle_rounds=1, had_message=False, now=datetime(2026, 7, 10, 9)) == 30
    assert next_poll_seconds(idle_rounds=20, had_message=False, now=datetime(2026, 7, 10, 9)) == 120
    assert next_poll_seconds(idle_rounds=0, had_message=True, now=datetime(2026, 7, 10, 1)) == 300


def test_retry_is_unbounded_but_capped_at_30_minutes():
    assert retry_delay_seconds(1) == 60
    assert retry_delay_seconds(2) == 300
    assert retry_delay_seconds(3) == 900
    assert retry_delay_seconds(99) == 1800


def test_sla_actions():
    assert sla_action(age_seconds=9, ack_sent=False, partial_sent=False, has_evidence=False) is None
    assert sla_action(age_seconds=10, ack_sent=False, partial_sent=False, has_evidence=False) == "ack"
    assert sla_action(age_seconds=45, ack_sent=True, partial_sent=False, has_evidence=True) == "partial"
    assert sla_action(age_seconds=45, ack_sent=True, partial_sent=False, has_evidence=False) == "status"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -q tests/test_scheduler_policy.py`

- [ ] **Step 3: Implement pure policies**

```python
# scheduler/policy.py
from __future__ import annotations


def is_quiet_hour(now) -> bool:
    return 0 <= now.hour < 7


def next_poll_seconds(idle_rounds: int, had_message: bool, now) -> int:
    if is_quiet_hour(now):
        return 300
    if had_message:
        return 15
    if idle_rounds <= 2:
        return 30
    return min(120, 30 * (2 ** min(idle_rounds - 2, 2)))


def retry_delay_seconds(attempts: int) -> int:
    schedule = (60, 300, 900, 1800)
    return schedule[min(max(attempts, 1), len(schedule)) - 1]


def sla_action(age_seconds: float, ack_sent: bool, partial_sent: bool, has_evidence: bool):
    if age_seconds >= 45 and not partial_sent:
        return "partial" if has_evidence else "status"
    if age_seconds >= 10 and not ack_sent:
        return "ack"
    return None
```

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest -q tests/test_scheduler_policy.py`

- [ ] **Step 5: Commit**

```powershell
git add scheduler/policy.py tests/test_scheduler_policy.py
git commit -m "feat: add adaptive polling retry and SLA policies"
```

---

### Task 3: WebDAV ingestion into the queue

**Files:**
- Create: `scheduler/ingest.py`
- Create: `tests/test_scheduler_ingest.py`
- Modify: `brain/pull.py`

- [ ] **Step 1: Write failing ingestion tests**

```python
from scheduler.db import QueueDB
from scheduler.ingest import enqueue_new_records


def test_only_records_after_completed_cursor_are_enqueued(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    records = [
        {"mid": 8, "conv_id": "u2", "direction": "in"},
        {"mid": 9, "conv_id": "u2", "direction": "out"},
        {"mid": 10, "conv_id": "u2", "direction": "in"},
        {"mid": 11, "conv_id": "u2", "direction": "in"},
    ]
    ids = enqueue_new_records(db, records, last_processed_mid=10, detected_at=1000)
    assert len(ids) == 1
    assert db.get(ids[0])["mid"] == 11


def test_repeated_ingestion_does_not_duplicate(tmp_path):
    db = QueueDB(tmp_path / "queue.db")
    records = [{"mid": 11, "conv_id": "u2", "direction": "in"}]
    assert enqueue_new_records(db, records, 10, 1000) == enqueue_new_records(db, records, 10, 2000)
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_scheduler_ingest.py`

- [ ] **Step 3: Implement ingestion**

`scheduler/ingest.py` must expose:

```python
def enqueue_new_records(db, records: list, last_processed_mid: int, detected_at: int) -> list:
    candidates = [
        r for r in records
        if r.get("direction") == "in"
        and isinstance(r.get("mid"), int)
        and r["mid"] > last_processed_mid
    ]
    candidates.sort(key=lambda r: r["mid"])
    return [db.enqueue(r, detected_at) for r in candidates]
```

Add a public `read_jsonl(path) -> list` to `brain/pull.py` or reuse `brain.context._read_jsonl` after renaming it public. Do not duplicate JSONL parsing.

Add `ingest_downloaded_conversations(db, inbound_dir, state, detected_at) -> int`, iterating `*.jsonl`, using each conversation’s `last_processed_mid`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_scheduler_ingest.py tests/test_pull.py tests/test_context.py`

- [ ] **Step 5: Commit**

```powershell
git add scheduler/ingest.py brain/pull.py brain/context.py tests/test_scheduler_ingest.py tests/test_context.py
git commit -m "feat: ingest new WebDAV messages into durable queue"
```

---

### Task 4: SLA notifier without advancing conversation cursor

**Files:**
- Create: `scheduler/notifier.py`
- Create: `tests/test_scheduler_notifier.py`

- [ ] **Step 1: Write failing tests**

```python
from scheduler.notifier import render_ack, render_partial, target_from_conv


def test_target_from_conversation():
    assert target_from_conv("u2") == {"uid": 2}
    assert target_from_conv("g5") == {"gid": 5}


def test_ack_is_short_and_non_final():
    assert render_ack() == "已收到，正在处理，稍后给你完整回复。"


def test_partial_uses_saved_evidence():
    text = render_partial([
        {"source": "中央气象台", "title": "台风预警", "summary": "预计影响华东沿海"}
    ])
    assert "中央气象台" in text
    assert "仍在补充" in text
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_scheduler_notifier.py`

- [ ] **Step 3: Implement deterministic notifications**

`scheduler/notifier.py` must:

- Parse `u<uid>` / `g<gid>` and reject unknown prefixes.
- Use `send.send_message` directly.
- Never call `reply_and_record.py`.
- Never update `state.json` or append to `history/*.jsonl`.
- Render at most 3 evidence items; each summary limited to 160 characters.
- Mark `ack_sent_at` or `partial_sent_at` only after HTTP success.

Expose:

```python
def target_from_conv(conv_id: str) -> dict: ...
def render_ack() -> str: ...
def render_status() -> str: ...
def render_partial(evidence: list) -> str: ...
def send_notification(server, api_key, conv_id, text, markdown=False): ...
def process_due_notifications(db, server, api_key, now_ms: int) -> int: ...
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_scheduler_notifier.py tests/test_send.py`

- [ ] **Step 5: Commit**

```powershell
git add scheduler/notifier.py tests/test_scheduler_notifier.py
git commit -m "feat: add idempotent 10s ack and 45s partial notifications"
```

---

### Task 5: HTTP fast path and progressive evidence

**Files:**
- Create: `scheduler/online.py`
- Create: `scripts/online_fetch.py`
- Create: `tests/test_scheduler_online.py`

- [ ] **Step 1: Write failing HTTP tests with mocked responses**

```python
import responses

from scheduler.online import fetch_json, fetch_text


@responses.activate
def test_fetch_json_returns_structured_evidence():
    responses.add(responses.GET, "https://example.com/weather.json",
                  json={"temperature": 30}, status=200)
    result = fetch_json("https://example.com/weather.json", timeout=5)
    assert result["kind"] == "json"
    assert result["data"]["temperature"] == 30


@responses.activate
def test_fetch_text_strips_markup_and_limits_output():
    responses.add(responses.GET, "https://example.com/page",
                  body="<html><title>T</title><body>Hello <b>world</b></body></html>",
                  status=200)
    result = fetch_text("https://example.com/page", timeout=5, max_chars=100)
    assert result["title"] == "T"
    assert "Hello world" in result["summary"]
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_scheduler_online.py`

- [ ] **Step 3: Implement bounded HTTP fetch**

Implement in `scheduler/online.py`:

```python
def fetch_json(url: str, timeout: float = 8) -> dict: ...
def fetch_text(url: str, timeout: float = 8, max_chars: int = 4000) -> dict: ...
def classify_response(response) -> str: ...
```

Constraints:

- `requests.Session` with explicit connect/read timeout tuple.
- Maximum response body 2 MiB; reject larger content.
- Accept only `http` and `https`.
- Strip scripts/styles and collapse whitespace using standard `html.parser`.
- Return evidence keys: `source`, `url`, `title`, `summary`, `kind`, `data`.
- On JS shell/captcha/login detection return `{"fallback": "browser"}` rather than hanging.

`scripts/online_fetch.py` CLI:

```text
python scripts/online_fetch.py json <url> --job-id 12
python scripts/online_fetch.py text <url> --job-id 12
```

When `--job-id` is present, append successful evidence to SQLite immediately.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_scheduler_online.py`

- [ ] **Step 5: Commit**

```powershell
git add scheduler/online.py scripts/online_fetch.py tests/test_scheduler_online.py
git commit -m "feat: add bounded HTTP fast path with progressive evidence"
```

---

### Task 6: Cursor queue CLI and consumer workflow

**Files:**
- Create: `scripts/queue_cli.py`
- Create: `tests/test_queue_cli.py`
- Create: `skill/queue_consumer.md`
- Modify: `skill/loop_prompt.md`

- [ ] **Step 1: Write failing CLI tests**

Test these commands against a temporary DB:

```python
def test_next_claims_job(queue_cli, seeded_db):
    result = queue_cli(["--db", str(seeded_db), "next", "--owner", "cursor-test"])
    assert result == 0


def test_fail_schedules_retry(queue_cli, seeded_processing_job):
    result = queue_cli([
        "--db", str(seeded_processing_job.db),
        "fail", "--job-id", str(seeded_processing_job.id),
        "--owner", "cursor-test", "--error", "browser timeout",
    ])
    assert result == 0
    assert seeded_processing_job.reload()["status"] == "retry_wait"
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_queue_cli.py`

- [ ] **Step 3: Implement queue CLI**

Commands:

```text
queue_cli.py next --owner <name> [--limit 3]
queue_cli.py renew --job-id N --owner <name>
queue_cli.py evidence --job-id N --file evidence.json
queue_cli.py complete --job-id N --owner <name>
queue_cli.py fail --job-id N --owner <name> --error TEXT
queue_cli.py cancel --job-id N
queue_cli.py list [--status pending]
```

`next` prints one JSON object per claimed job. `fail` uses `retry_delay_seconds(attempts + 1)`.

- [ ] **Step 4: Write consumer instructions**

`skill/queue_consumer.md` must require:

1. Claim up to 3 jobs from different conversations.
2. Run `build_context.py --conv`.
3. Classify `network_mode`.
4. Prefer `online_fetch.py`; write each evidence result immediately.
5. Use browser-use only on explicit fallback.
6. Renew lease during work.
7. Call existing `reply_and_record.py` for final response.
8. Only after exit code 0 call `queue_cli.py complete`.
9. On any error call `queue_cli.py fail`.

Replace fixed polling in `skill/loop_prompt.md` with a pointer to `skill/queue_consumer.md`; retain old manual flow under an “应急模式” heading.

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_queue_cli.py`

- [ ] **Step 6: Commit**

```powershell
git add scripts/queue_cli.py tests/test_queue_cli.py skill/queue_consumer.md skill/loop_prompt.md
git commit -m "feat: add Cursor queue consumer workflow and lease CLI"
```

---

### Task 7: Scheduler service loop

**Files:**
- Create: `scheduler/service.py`
- Create: `scripts/scheduler.py`
- Create: `tests/test_scheduler_service.py`

- [ ] **Step 1: Write replay-oriented failing tests**

```python
def test_poll_continues_while_job_is_processing(service, fake_clock):
    service.enqueue_message(mid=1431, conv_id="u2")
    service.db.claim("cursor", fake_clock.now_ms(), 1, 3600)
    service.enqueue_message(mid=1432, conv_id="u2")
    service.enqueue_message(mid=2001, conv_id="u5")
    assert service.db.find("u2", 1432)["status"] == "pending"
    assert service.db.find("u5", 2001)["status"] == "pending"


def test_ack_and_partial_deadlines_do_not_stop_polling(service, fake_clock):
    job = service.enqueue_message(mid=1431, conv_id="u2")
    fake_clock.advance(seconds=10)
    service.tick()
    assert service.db.get(job)["ack_sent_at"] is not None
    fake_clock.advance(seconds=35)
    service.tick()
    assert service.db.get(job)["partial_sent_at"] is not None
    assert service.poll_count >= 2
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_scheduler_service.py`

- [ ] **Step 3: Implement service**

`SchedulerService.tick()` order:

1. Recover expired leases.
2. Pull changed WebDAV conversations.
3. Ingest new inbound records.
4. Process due 10s/45s notifications.
5. Compute next interval.
6. Persist health snapshot (`last_tick_at`, `next_tick_at`, counts).

`run_forever()`:

- Use a single-instance lock file with PID.
- Catch per-tick exceptions, log, and continue after retry delay.
- Handle SIGINT/CTRL+C cleanly.
- Never execute Cursor inference.

`scripts/scheduler.py` commands:

```text
python scripts/scheduler.py run
python scripts/scheduler.py once
python scripts/scheduler.py health
python scripts/scheduler.py init-db
```

- [ ] **Step 4: Run focused and full tests**

Run: `python -m pytest -q tests/test_scheduler_service.py`

Then: `python -m pytest -q`

- [ ] **Step 5: Commit**

```powershell
git add scheduler/service.py scripts/scheduler.py tests/test_scheduler_service.py
git commit -m "feat: add independent scheduler service loop"
```

---

### Task 8: Windows Task Scheduler lifecycle

**Files:**
- Create: `scripts/scheduler_install.ps1`
- Create: `scripts/scheduler_start.ps1`
- Create: `scripts/scheduler_stop.ps1`
- Create: `scripts/scheduler_status.ps1`
- Create: `scripts/scheduler_uninstall.ps1`
- Create: `tests/test_scheduler_scripts.py`

- [ ] **Step 1: Write script contract tests**

Tests read scripts as text and verify:

- Task name is consistently `AnsweringMachineScheduler`.
- Working directory is repository root.
- Action executes `python scripts/scheduler.py run`.
- Restart count and restart interval are configured.
- Install script refuses duplicate task unless `-Force`.
- Status script prints scheduled-task state and scheduler health.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_scheduler_scripts.py`

- [ ] **Step 3: Implement PowerShell scripts**

Installation behavior:

- Resolve repository and Python paths to absolute paths.
- Create task on user logon.
- Set `MultipleInstances=IgnoreNew`.
- Restart after failure every 1 minute, up to 999 attempts.
- Set working directory to repository root.
- Do not embed credentials in task arguments.

Management behavior:

- `start`: `Start-ScheduledTask`.
- `stop`: `Stop-ScheduledTask`.
- `status`: scheduled task state + `python scripts/scheduler.py health`.
- `uninstall`: stop then unregister, requiring explicit confirmation unless `-Force`.

- [ ] **Step 4: Run tests and dry-run validation**

Run: `python -m pytest -q tests/test_scheduler_scripts.py`

Run install script with `-WhatIf`; verify no task mutation.

- [ ] **Step 5: Commit**

```powershell
git add scripts/scheduler_*.ps1 tests/test_scheduler_scripts.py
git commit -m "feat: add Windows scheduled-task lifecycle scripts"
```

---

### Task 9: Configuration, documentation and end-to-end replay

**Files:**
- Modify: `.env.example`
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `docs/TODO.md`
- Create: `tests/test_scheduler_replay.py`

- [ ] **Step 1: Add configuration**

Add to `.env.example`:

```dotenv
SCHEDULER_DB=data/queue.db
SCHEDULER_ACTIVE_INTERVAL=15
SCHEDULER_NORMAL_INTERVAL=30
SCHEDULER_IDLE_MAX_INTERVAL=120
SCHEDULER_QUIET_START=00:00
SCHEDULER_QUIET_END=07:00
SCHEDULER_QUIET_INTERVAL=300
SCHEDULER_ACK_SECONDS=10
SCHEDULER_PARTIAL_SECONDS=45
SCHEDULER_MAX_CONCURRENCY=3
SCHEDULER_LEASE_SECONDS=120
```

Add `data/logs/`, `*.db-wal`, and `*.db-shm` to `.gitignore` (the whole `data/` directory is already ignored; explicit patterns document intent).

- [ ] **Step 2: Add exact replay test**

`tests/test_scheduler_replay.py` must reproduce:

1. Enqueue `1431` at `23:50:49`.
2. Lease it to a stalled Cursor worker.
3. Advance 10 seconds: ack is due exactly once.
4. Enqueue `1432` and `1433` while `1431` remains processing.
5. Advance to 45 seconds: partial/status is due.
6. Expire the lease: `1431` returns to retry.
7. Reclaim and complete all three in FIFO order.
8. Verify final replies and notifications are not duplicated.

- [ ] **Step 3: Document operations**

README sections:

- Architecture diagram: receiver → scheduler → SQLite → Cursor consumer.
- Install/start/stop/status/uninstall.
- SLA definition and the fact it starts after detection.
- Quiet-hours behavior.
- Cursor offline behavior.
- HTTP fast path vs browser-use fallback.
- Troubleshooting queue backlog, expired leases and task status.

Mark the TODO item completed only after the end-to-end replay and real scheduled-task smoke test pass.

- [ ] **Step 4: Run full verification**

Run:

```powershell
python -m pytest -q
python scripts/scheduler.py init-db
python scripts/scheduler.py once
powershell -File scripts/scheduler_install.ps1 -WhatIf
```

Expected:

- All tests pass.
- DB initializes.
- One scheduler tick completes without sending duplicates.
- `-WhatIf` reports intended task configuration without installing it.

- [ ] **Step 5: Commit**

```powershell
git add .env.example .gitignore README.md docs/TODO.md tests/test_scheduler_replay.py
git commit -m "docs: add reliable scheduler operations and replay acceptance"
```

---

## Implementation checkpoints

1. **Checkpoint A (Tasks 1–4):** Queue durability, policies, ingestion and SLA notifier; no daemon installation yet.
2. **Checkpoint B (Tasks 5–7):** HTTP fast path, Cursor consumer and independent service loop; replay test available.
3. **Checkpoint C (Tasks 8–9):** Windows lifecycle, docs and real smoke test.

At each checkpoint run `python -m pytest -q` and request code review before proceeding.

## Self-review

### Spec coverage

- Root cause decoupling: Tasks 1, 3, 7.
- SQLite queue/leases/idempotency: Task 1.
- Adaptive/quiet/retry/SLA: Task 2.
- 10s ack/45s partial: Task 4.
- HTTP fast path: Task 5.
- Cursor consumer and 3-way per-conversation concurrency: Task 6.
- Independent daemon: Task 7.
- Windows auto-start/recovery: Task 8.
- Observability/config/replay: Task 9.

### Placeholder scan

No TBD or deferred implementation steps. Every task names exact files, commands, expected results and required interfaces.

### Consistency

- Status values match the design schema.
- SLA always starts at `detected_at`.
- Retry is unlimited with 30-minute cap.
- Occupancy/partial notifications never advance `state.json`.
- Final reply remains the only path that records history and advances conversation state.
