# tests/test_factory.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import receiver


def test_app_factory_builds_running_app(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_UID", "7")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    app = receiver.app_factory()
    assert isinstance(app, FastAPI)
    client = TestClient(app)
    assert client.get("/").text == "ok"
    assert client.get("/health").json() == {"status": "healthy"}
