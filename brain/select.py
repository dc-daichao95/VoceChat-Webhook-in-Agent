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
