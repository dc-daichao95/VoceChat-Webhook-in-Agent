# tests/test_config.py
import pytest

from app.config import load_config


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("BOT_UID", "123")
    monkeypatch.setenv("SCOPE_DM", "false")
    monkeypatch.setenv("RAW_DUMP", "true")
    monkeypatch.setenv("DATA_DIR", "/tmp/x")
    cfg = load_config(env_path=None)
    assert cfg.bot_uid == 123
    assert cfg.scope_dm is False
    assert cfg.scope_group_mention is True  # default
    assert cfg.raw_dump is True
    assert cfg.data_dir == "/tmp/x"


def test_load_config_missing_bot_uid(monkeypatch):
    monkeypatch.delenv("BOT_UID", raising=False)
    with pytest.raises(ValueError):
        load_config(env_path=None)
