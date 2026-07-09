# app/receiver.py
"""dumb 接收器:FastAPI 应用,只负责「收 webhook → 过滤 → 落盘」,不发送、不生成回复。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import filters, storage
from .config import Config

log = logging.getLogger("receiver")


def create_app(config: Config) -> FastAPI:
    """构建 FastAPI 应用;显式注入 config 便于测试传入不同配置。

    POST 处理刻意「吞掉」异常并始终回 200,避免 VoceChat 因错误重试而放大问题。
    """
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


def app_factory() -> FastAPI:
    """无参工厂:从环境加载配置再建应用,供 `uvicorn app.receiver:app_factory --factory` 启动。"""
    from .config import load_config

    return create_app(load_config())


def _process(config: Config, payload: dict) -> None:
    """单条 payload 处理管线:可选原始转储 → 受理过滤 → mid 去重 → 追加落盘。"""
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
