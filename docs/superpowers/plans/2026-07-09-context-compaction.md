# 上下文压缩 / 超长历史 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) + superpowers:test-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** 让单会话喂给回复生成的上下文有界(事实卡片 + 滚动摘要 + 最近 N 条),超阈值时 gzip 归档旧记录并从活跃 JSONL 移除,自动融入 /loop。

**Architecture:** 纯逻辑放 `brain/`(可测),CLI 放 `scripts/`;摘要/卡片由大脑在 loop 内产出,脚本负责检测/归档/截断/落盘。

**Tech Stack:** Python 3.8(现有 test env)、pytest、gzip、json。

关联 spec:`docs/superpowers/specs/2026-07-09-context-compaction-design.md`

---

### Task 1: brain/compaction.py — 常量与切分纯逻辑(TDD)

**Files:** Create `brain/compaction.py`、`tests/test_compaction.py`

- [ ] **Step 1: 写失败测试** `tests/test_compaction.py`
```python
from brain import compaction


def test_split_recent_keeps_last_n():
    recs = [{"mid": i} for i in range(50)]
    old, recent = compaction.split_recent(recs, keep=20)
    assert len(recent) == 20 and recent[0]["mid"] == 30
    assert len(old) == 30 and old[-1]["mid"] == 29


def test_split_recent_under_keep_returns_no_old():
    recs = [{"mid": i} for i in range(5)]
    old, recent = compaction.split_recent(recs, keep=20)
    assert old == [] and len(recent) == 5


def test_needs_compaction():
    assert compaction.needs_compaction(41, trigger=40) is True
    assert compaction.needs_compaction(40, trigger=40) is False
```

- [ ] **Step 2: 运行,确认失败** `python -m pytest -q tests/test_compaction.py`(Expected: import/attr 失败)

- [ ] **Step 3: 实现** `brain/compaction.py`
```python
# brain/compaction.py
"""超长历史压缩的常量与纯逻辑:切分"旧/最近 N"、判定是否需压缩。"""
from __future__ import annotations

RECENT_KEEP = 20        # 活跃 JSONL 保留的最近条数
COMPACT_TRIGGER = 40    # raw 条数 > 该值触发压缩
SUMMARY_SOFT_LIMIT = 1500  # 摘要软上限(字符),大脑自控


def split_recent(records: list, keep: int = RECENT_KEEP):
    """切分为(旧, 最近 keep 条);不足 keep 条时旧为空。"""
    if len(records) <= keep:
        return [], list(records)
    return list(records[:-keep]), list(records[-keep:])


def needs_compaction(raw_count: int, trigger: int = COMPACT_TRIGGER) -> bool:
    """活跃条数严格大于阈值才压缩。"""
    return raw_count > trigger
```

- [ ] **Step 4: 运行,确认通过** `python -m pytest -q tests/test_compaction.py`

- [ ] **Step 5: Commit**
```
git add brain/compaction.py tests/test_compaction.py
git commit -m "feat: add compaction pure logic (split_recent, needs_compaction)"
```

---

### Task 2: brain/context.py — 上下文组装(TDD)

**Files:** Create `brain/context.py`、`tests/test_context.py`

- [ ] **Step 1: 写失败测试** `tests/test_context.py`
```python
import json
from brain import context


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_build_context_full(tmp_path):
    hist = tmp_path
    _write(hist / "u1.facts.json", json.dumps({"name": "dc", "language": "zh"}))
    _write(hist / "u1.summary.md", "早期聊了天气。")
    _write(hist / "u1.jsonl", "\n".join(json.dumps({"mid": i, "direction": "in", "content": f"m{i}"}) for i in range(3)))
    ctx = context.build_context("u1", str(hist), recent_keep=20)
    assert ctx["facts"]["name"] == "dc"
    assert "天气" in ctx["summary"]
    assert len(ctx["recent"]) == 3


def test_build_context_missing_degrades(tmp_path):
    ctx = context.build_context("u9", str(tmp_path), recent_keep=20)
    assert ctx["facts"] == {} and ctx["summary"] == "" and ctx["recent"] == []


def test_build_context_recent_truncation(tmp_path):
    hist = tmp_path
    _write(hist / "u2.jsonl", "\n".join(json.dumps({"mid": i}) for i in range(30)))
    ctx = context.build_context("u2", str(hist), recent_keep=20)
    assert len(ctx["recent"]) == 20 and ctx["recent"][0]["mid"] == 10
```

- [ ] **Step 2: 运行,确认失败**

- [ ] **Step 3: 实现** `brain/context.py`
```python
# brain/context.py
"""组装回复上下文:事实卡片 + 滚动摘要 + 最近 N 条,取代全量读历史。"""
from __future__ import annotations

import json
from pathlib import Path

from brain.compaction import RECENT_KEEP


def _read_jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build_context(conv_id: str, history_dir: str, recent_keep: int = RECENT_KEEP) -> dict:
    """载入 facts/summary/最近 N 条;任一缺失优雅降级为空。"""
    base = Path(history_dir)
    facts_p = base / f"{conv_id}.facts.json"
    summary_p = base / f"{conv_id}.summary.md"
    jsonl_p = base / f"{conv_id}.jsonl"

    facts = {}
    if facts_p.exists():
        try:
            facts = json.loads(facts_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            facts = {}
    summary = summary_p.read_text(encoding="utf-8").strip() if summary_p.exists() else ""
    recent = _read_jsonl(jsonl_p)[-recent_keep:]
    return {"facts": facts, "summary": summary, "recent": recent}


def render(ctx: dict) -> str:
    """把上下文渲染为供阅读的文本块(卡片 → 摘要 → 最近逐字)。"""
    parts = []
    if ctx.get("facts"):
        parts.append("## 用户事实卡片\n" + json.dumps(ctx["facts"], ensure_ascii=False, indent=2))
    if ctx.get("summary"):
        parts.append("## 早期对话摘要\n" + ctx["summary"])
    if ctx.get("recent"):
        lines = [json.dumps(r, ensure_ascii=False) for r in ctx["recent"]]
        parts.append("## 最近对话(逐字)\n" + "\n".join(lines))
    return "\n\n".join(parts)
```

- [ ] **Step 4: 运行,确认通过**

- [ ] **Step 5: Commit**
```
git add brain/context.py tests/test_context.py
git commit -m "feat: add context assembly (facts + summary + recent N)"
```

---

### Task 3: scripts/compact.py — 检测 / 归档截断 CLI(含归档逻辑测试)

**Files:** Create `scripts/compact.py`、`tests/test_compact_apply.py`

- [ ] **Step 1: 写失败测试** `tests/test_compact_apply.py`(测可复用的归档函数)
```python
import gzip
import json
from pathlib import Path

import compact  # scripts/ 在 sys.path


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
```

- [ ] **Step 2: 运行,确认失败**

- [ ] **Step 3: 实现** `scripts/compact.py`(纯归档函数 + argparse CLI）
```python
#!/usr/bin/env python3
"""检测并压缩超长会话历史:归档旧记录(gzip)、活跃 JSONL 截到最近 N。

摘要/卡片由大脑在 loop 内用 Write 更新;本脚本只做检测、归档、截断、落盘。
用法:
  python scripts/compact.py --check
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
from brain.context import _read_jsonl  # noqa: E402

HISTORY_DIR = REPO / "data" / "history"
ARCHIVE_DIR = REPO / "data" / "archive"


def archive_and_truncate(conv_id: str, history_dir: str, archive_dir: str, keep: int = compaction.RECENT_KEEP) -> int:
    """原子归档:先写 gzip 成功,再把活跃 JSONL 重写为最近 keep 条。返回归档条数。"""
    jsonl = Path(history_dir) / f"{conv_id}.jsonl"
    records = _read_jsonl(jsonl)
    old, recent = compaction.split_recent(records, keep=keep)
    if not old:
        return 0
    out_dir = Path(archive_dir) / conv_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    gz = out_dir / f"{ts}.jsonl.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as f:
        for r in old:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 归档成功后才截断,保证失败不丢数据
    tmp = jsonl.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recent), encoding="utf-8")
    tmp.replace(jsonl)
    return len(old)


def _iter_convs(history_dir: Path):
    for p in sorted(history_dir.glob("*.jsonl")):
        yield p.stem


def cmd_check(args) -> int:
    hist = HISTORY_DIR
    if not hist.exists():
        print("no history dir")
        return 0
    convs = [args.conv] if args.conv else list(_iter_convs(hist))
    pending = []
    for conv in convs:
        n = len(_read_jsonl(hist / f"{conv}.jsonl"))
        if compaction.needs_compaction(n):
            pending.append((conv, n))
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
```

- [ ] **Step 4: 运行,确认通过** `python -m pytest -q tests/test_compact_apply.py`

- [ ] **Step 5: Commit**
```
git add scripts/compact.py tests/test_compact_apply.py
git commit -m "feat: add compact CLI (check + gzip archive + truncate)"
```

---

### Task 4: build_context CLI + loop_prompt 集成

**Files:** Create `scripts/build_context.py`;Modify `skill/loop_prompt.md`

- [ ] **Step 1:** `scripts/build_context.py`
```python
#!/usr/bin/env python3
"""打印某会话组装后的回复上下文(供大脑在 /loop 步骤 2a 读取)。"""
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
    ap.add_argument("--conv", required=True)
    args = ap.parse_args(argv)
    ctx = context.build_context(args.conv, str(HISTORY_DIR))
    print(context.render(ctx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2:** 改 `skill/loop_prompt.md`:
  - 步骤 2a:把"读 `data/history/<conv_id>.jsonl`(全量)作上下文"改为
    "运行 `python scripts/build_context.py --conv <conv_id>` 得到 事实卡片+摘要+最近 N 作上下文"。
  - 新增步骤 4「每轮末压缩」:
    1. `python scripts/compact.py --check`;
    2. 对每个 `COMPACT_NEEDED <conv>`:读旧记录(`data/history/<conv>.jsonl` 前部)+ 现有 `summary.md`/`facts.json`,用 Write 更新 `data/history/<conv>.summary.md`(≤~1500 字,融合旧信息)与 `data/history/<conv>.facts.json`(称呼/语言/偏好/禁忌);
    3. `python scripts/compact.py --apply --conv <conv>`;
    4. 单会话失败只跳过。

- [ ] **Step 3:** 冒烟:构造一个 >40 条的 `data/history/_smoke.jsonl`,`build_context --conv _smoke` 只显示最近 20;`compact.py --check` 报 `COMPACT_NEEDED`;`--apply` 后活跃=20、archive 有 gz;清理临时会话。

- [ ] **Step 4:** Commit
```
git add scripts/build_context.py skill/loop_prompt.md
git commit -m "feat: wire context compaction into /loop (build_context + compact steps)"
```

---

### Task 5: 收尾

- [ ] **Step 1:** `python -m pytest -q` 全绿。
- [ ] **Step 2:** finishing-a-development-branch:合并/PR/保留/丢弃四选项。

---

## Self-Review

**Spec coverage:** §3 数据模型→Task 2/3 产物;§4 阈值→Task 1 常量;§5.1 context→Task 2;§5.2 compact→Task 3;§5.3 build_context→Task 4;§6 流程→Task 4;§8 测试→各 Task 的 pytest 步。

**Placeholder scan:** 无 TBD;每个代码步给出完整代码。

**Type consistency:** `split_recent`/`needs_compaction`/`RECENT_KEEP`/`COMPACT_TRIGGER` 命名在 compaction.py 定义,context.py 与 compact.py 一致引用;`_read_jsonl` 在 context.py 定义并被 compact.py 复用;归档函数名 `archive_and_truncate` 测试与实现一致。

**TDD:** Task 1/2/3 均先写失败测试再实现。测试用 `tmp_path`,不依赖真实 data/。
