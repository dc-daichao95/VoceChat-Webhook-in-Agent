# app/filters.py
"""接收器的纯函数:判定 webhook 是否受理、推导会话 ID、构建入站记录。

抽为无副作用纯函数,便于单测覆盖各类 payload,不与落盘/网络耦合。
"""
from __future__ import annotations

import time

TEXT_TYPES = {"text/plain", "text/markdown"}


def is_normal_text(payload: dict) -> bool:
    """仅接受普通文本/Markdown 消息;过滤掉系统事件、图片等非文本类型。"""
    detail = payload.get("detail") or {}
    return detail.get("type") == "normal" and detail.get("content_type") in TEXT_TYPES


def conv_id_of(payload: dict):
    """推导会话 ID:私聊用 u<from_uid>、群聊用 g<gid>;无法判定返回 None。"""
    target = payload.get("target") or {}
    if "uid" in target:
        return f"u{payload.get('from_uid')}"
    if "gid" in target:
        return f"g{target['gid']}"
    return None


def mentioned_uids(payload: dict) -> list:
    """提取被 @ 的 uid 列表;非法项静默跳过,避免坏数据中断处理。"""
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
    """受理判定:文本消息、非 bot 自己所发;私聊看 scope_dm,群聊需被 @ 且开启群作用域。

    过滤掉 bot 自己的消息是防自我循环的关键。
    """
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
    """把 webhook payload 归一为落盘用的入站记录;recorded_at 记录本地入库时刻(毫秒)。"""
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
