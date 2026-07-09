# brain/select.py
"""大脑侧纯函数:从会话历史中选出尚未处理的入站消息。"""
from __future__ import annotations


def select_pending(conv_id: str, records: list, last_processed_mid: int, seen_mids: set) -> list:
    """筛出待回复的入站消息:仅 in 方向、mid 大于游标且未在 seen 中,按 mid 升序返回。

    双重判据(游标 + seen)兼顾顺序推进与乱序去重,避免重复回复。
    """
    pending = []
    for r in records:
        if r.get("direction") != "in":
            continue
        mid = r.get("mid")
        if mid is None or mid <= last_processed_mid or mid in seen_mids:
            continue
        pending.append(r)
    return sorted(pending, key=lambda r: r["mid"])
