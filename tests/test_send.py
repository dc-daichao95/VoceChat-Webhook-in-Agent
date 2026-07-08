# tests/test_send.py
import responses

import send


def test_build_url_uid():
    assert send.build_url("https://chat.example.com/", uid=1) == "https://chat.example.com/api/bot/send_to_user/1"


def test_build_url_gid():
    assert send.build_url("https://chat.example.com", gid=2) == "https://chat.example.com/api/bot/send_to_group/2"


@responses.activate
def test_send_message_uid_text():
    responses.add(responses.POST, "https://chat.example.com/api/bot/send_to_user/1", body="ok", status=200)
    r = send.send_message("https://chat.example.com", "KEY", "hello", uid=1)
    assert r.status_code == 200
    sent = responses.calls[0].request
    assert sent.headers["x-api-key"] == "KEY"
    assert sent.headers["content-type"] == "text/plain"
    assert sent.body == b"hello"


@responses.activate
def test_send_message_gid_markdown():
    responses.add(responses.POST, "https://chat.example.com/api/bot/send_to_group/2", body="ok", status=200)
    send.send_message("https://chat.example.com", "KEY", "**hi**", gid=2, markdown=True)
    assert responses.calls[0].request.headers["content-type"] == "text/markdown"


@responses.activate
def test_main_missing_env_returns_2(monkeypatch):
    monkeypatch.delenv("VOCECHAT_SERVER_URL", raising=False)
    monkeypatch.delenv("VOCECHAT_API_KEY", raising=False)
    assert send.main(["--target-uid", "1", "--text", "hi"]) == 2
