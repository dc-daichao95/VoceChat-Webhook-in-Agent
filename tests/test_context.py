import json

from brain import context


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_build_context_full(tmp_path):
    hist = tmp_path
    _write(hist / "u1.facts.json", json.dumps({"name": "dc", "language": "zh"}))
    _write(hist / "u1.summary.md", "早期聊了天气。")
    _write(hist / "u1.jsonl", "\n".join(json.dumps({"mid": i, "direction": "in", "content": f"m{i}"}) for i in range(3)))
    ctx = context.build_context("u1", str(hist), recent_keep=20)
    assert ctx["facts"]["name"] == "dc"
    assert "天气" in ctx["summary"]
    assert len(ctx["recent"]) == 3


def test_build_context_missing_degrades(tmp_path):
    ctx = context.build_context("u9", str(tmp_path), recent_keep=20)
    assert ctx["facts"] == {} and ctx["summary"] == "" and ctx["recent"] == []


def test_build_context_recent_truncation(tmp_path):
    hist = tmp_path
    _write(hist / "u2.jsonl", "\n".join(json.dumps({"mid": i}) for i in range(30)))
    ctx = context.build_context("u2", str(hist), recent_keep=20)
    assert len(ctx["recent"]) == 20 and ctx["recent"][0]["mid"] == 10
