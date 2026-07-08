# app/receiver.py
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import filters, storage
from .config import Config

log = logging.getLogger("receiver")


def create_app(config: Config) -> FastAPI:
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
    from .config import load_config

    return create_app(load_config())


def _process(config: Config, payload: dict) -> None:
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
