# brain/context.py
"""组装回复上下文:事实卡片 + 滚动摘要 + 最近 N 条,取代全量读历史。"""
from __future__ import annotations

import json
from pathlib import Path

from brain.compaction import RECENT_KEEP


def _read_jsonl(p: Path) -> list:
    """读 JSONL 为记录列表;文件缺失或坏行优雅跳过。"""
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
    facts = {}
    facts_p = base / f"{conv_id}.facts.json"
    if facts_p.exists():
        try:
            facts = json.loads(facts_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            facts = {}
    summary_p = base / f"{conv_id}.summary.md"
    summary = summary_p.read_text(encoding="utf-8").strip() if summary_p.exists() else ""
    recent = _read_jsonl(base / f"{conv_id}.jsonl")[-recent_keep:]
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
