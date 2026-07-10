"""Legacy reply sender safety-gate tests."""

from scripts import reply_and_record


class Response:
    ok = True
    text = "ok"


def test_legacy_send_is_refused_by_default(
    tmp_path, monkeypatch, capsys
):
    """Queue workflows cannot accidentally invoke the legacy sender."""
    reply_file = tmp_path / "reply.txt"
    reply_file.write_text("private", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("sender must not run")

    monkeypatch.setattr(reply_and_record.send, "send_message", forbidden)

    code = reply_and_record.main(
        ["--conv", "u1", "--mid", "1", "--reply-file", str(reply_file)]
    )

    assert code == 2
    assert "--allow-legacy-send" in capsys.readouterr().err


def test_explicit_legacy_gate_preserves_compatibility(
    tmp_path, monkeypatch, capsys
):
    """An operator can explicitly opt into the deprecated manual path."""
    reply_file = tmp_path / "reply.txt"
    reply_file.write_text("private", encoding="utf-8")
    recorded = []
    monkeypatch.setenv("VOCECHAT_SERVER_URL", "https://chat.example")
    monkeypatch.setenv("VOCECHAT_API_KEY", "key")
    monkeypatch.setattr(
        reply_and_record.send, "send_message",
        lambda *args, **kwargs: Response(),
    )
    monkeypatch.setattr(reply_and_record, "record_reply", recorded.append)

    code = reply_and_record.main(
        [
            "--conv", "u1", "--mid", "1", "--reply-file", str(reply_file),
            "--allow-legacy-send",
        ]
    )

    assert code == 0
    assert recorded[0]["reply"] == "private"
    assert "deprecated" in capsys.readouterr().err.lower()
