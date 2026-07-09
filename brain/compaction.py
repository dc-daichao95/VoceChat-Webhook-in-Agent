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
