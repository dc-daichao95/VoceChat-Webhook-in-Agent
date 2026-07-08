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
