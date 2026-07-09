# app/config.py
"""接收器配置:从环境变量(可选 .env)加载并校验为不可变的 Config。"""
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
    """加载并校验配置;BOT_UID 缺失即抛 ValueError(它是防自我循环/群 @ 判定的必需项)。"""
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
