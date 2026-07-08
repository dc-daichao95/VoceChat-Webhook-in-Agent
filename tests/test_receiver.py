# tests/test_receiver.py
from fastapi.testclient import TestClient

from app.config import Config
from app.receiver import create_app


def _cfg(tmp_path, raw_dump=False):
    return Config(bot_uid=0, scope_dm=True, scope_group_mention=True,
                  data_dir=str(tmp_path), raw_dump=raw_dump,
                  listen_host="0.0.0.0", listen_port=8091)


def _dm(mid=1, uid=7910, content="hi"):
    return {"mid": mid, "from_uid": uid, "created_at": 100,
            "detail": {"type": "normal", "content_type": "text/plain", "content": content, "properties": None},
            "target": {"uid": 999}}


def test_probe_and_health(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    assert c.get("/").text == "ok"
    assert c.get("/health").json() == {"status": "healthy"}


def test_post_accepts_and_persists_dm(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    r = c.post("/", json=_dm(mid=5))
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    p = tmp_path / "conversations" / "u7910.jsonl"
    assert p.exists() and '"mid": 5' in p.read_text(encoding="utf-8")


def test_post_ignores_own_message(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    c.post("/", json=_dm(mid=6, uid=0))
    assert not (tmp_path / "conversations").exists()


def test_post_dedup_same_mid(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    c.post("/", json=_dm(mid=7))
    c.post("/", json=_dm(mid=7))
    lines = (tmp_path / "conversations" / "u7910.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_post_bad_json_returns_200(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    r = c.post("/", data="not-json", headers={"content-type": "application/json"})
    assert r.status_code == 200


def test_raw_dump_writes_even_non_normal(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path, raw_dump=True)))
    payload = _dm(mid=8); payload["detail"]["type"] = "edit"
    c.post("/", json=payload)
    assert list((tmp_path / "raw").glob("*_8.json"))
    assert not (tmp_path / "conversations").exists()  # edit 不落 conversations
