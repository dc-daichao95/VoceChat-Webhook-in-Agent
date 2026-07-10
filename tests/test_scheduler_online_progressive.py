"""渐进证据编排和 online_fetch 命令行入口的行为测试。"""

import json

import pytest
import responses

import scheduler.online as online_module
from scheduler.db import QueueDB
from scheduler.online import gather_progressively
from scripts import online_fetch

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def public_http(monkeypatch):
    """为 CLI 的 responses 测试显式注入 DNS 与 peer 策略。"""
    real_json = online_module.fetch_json
    real_text = online_module.fetch_text
    resolver = lambda hostname, port: [PUBLIC_IP]
    peer_getter = lambda response: PUBLIC_IP
    monkeypatch.setattr(
        online_module,
        "fetch_json",
        lambda url, timeout: real_json(
            url, timeout, resolver=resolver, peer_getter=peer_getter
        ),
    )
    monkeypatch.setattr(
        online_module,
        "fetch_text",
        lambda url, timeout, max_chars=4000: real_text(
            url,
            timeout,
            max_chars,
            resolver=resolver,
            peer_getter=peer_getter,
        ),
    )


class FakeClock:
    """提供可手动推进的单调时钟。"""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        """推进测试时钟。"""
        self.now += seconds


def make_evidence(url, title="Result"):
    """构造完整的结构化证据。"""
    return {
        "source": "example.com",
        "url": url,
        "title": title,
        "summary": "useful",
        "kind": "text",
        "data": None,
    }


@pytest.mark.parametrize(
    "deadline,error_type",
    (
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (float("-inf"), ValueError),
        ("10", TypeError),
        (True, TypeError),
    ),
)
def test_gather_progressively_rejects_invalid_deadline(deadline, error_type):
    """绝对 deadline 必须是有限数值且不得接受布尔或字符串。"""
    calls = []

    with pytest.raises(error_type):
        gather_progressively(
            [{"kind": "json", "url": "https://example.com/api"}],
            deadline=deadline,
            clock=lambda: 0.0,
            fetchers={"json": lambda *args, **kwargs: calls.append(1)},
        )

    assert calls == []


@pytest.mark.parametrize(
    "value,error_name",
    (
        (float("nan"), "ValueError"),
        (float("inf"), "ValueError"),
        (float("-inf"), "ValueError"),
        ("5", "TypeError"),
        (True, "TypeError"),
    ),
)
def test_gather_progressively_isolates_invalid_source_timeout(
    value, error_name
):
    """来源 timeout 非有限数值或错误类型时不得调用抓取器。"""
    calls = []

    result = gather_progressively(
        [
            {
                "kind": "json",
                "url": "https://example.com/api",
                "timeout": value,
            }
        ],
        deadline=10,
        clock=lambda: 0.0,
        fetchers={"json": lambda *args, **kwargs: calls.append(1)},
    )

    assert calls == []
    assert result["errors"][0]["error"] == error_name
    assert result["status"] == "failed"


@pytest.mark.parametrize(
    "value,error_name",
    (
        (True, "TypeError"),
        (1.5, "TypeError"),
        ("100", "TypeError"),
        (0, "ValueError"),
        (-1, "ValueError"),
    ),
)
def test_gather_progressively_isolates_invalid_source_max_chars(
    value, error_name
):
    """文本来源 max_chars 非正整数时不得调用抓取器。"""
    calls = []

    result = gather_progressively(
        [
            {
                "kind": "text",
                "url": "https://example.com/page",
                "max_chars": value,
            }
        ],
        deadline=10,
        clock=lambda: 0.0,
        fetchers={"text": lambda *args, **kwargs: calls.append(1)},
    )

    assert calls == []
    assert result["errors"][0]["error"] == error_name
    assert result["status"] == "failed"


def test_gather_progressively_fetches_and_appends_in_source_order():
    """每项成功证据应在下一来源开始前立即追加。"""
    events = []

    def fetch_json_stub(url, timeout):
        events.append(("fetch", url, timeout))
        return make_evidence(url, "JSON")

    def fetch_text_stub(url, timeout, max_chars=4000):
        events.append(("fetch", url, timeout, max_chars))
        return make_evidence(url, "Text")

    def append(evidence):
        events.append(("append", evidence["title"]))

    result = gather_progressively(
        [
            {"kind": "json", "url": "https://example.com/one"},
            {
                "kind": "text",
                "url": "https://example.com/two",
                "max_chars": 200,
            },
        ],
        deadline=10,
        append_evidence=append,
        clock=lambda: 0.0,
        fetchers={"json": fetch_json_stub, "text": fetch_text_stub},
    )

    assert events == [
        ("fetch", "https://example.com/one", 8.0),
        ("append", "JSON"),
        ("fetch", "https://example.com/two", 8.0, 200),
        ("append", "Text"),
    ]
    assert result["status"] == "complete"
    assert result["persisted"] == 2
    assert [item["title"] for item in result["evidence"]] == ["JSON", "Text"]


def test_gather_progressively_isolates_failures_without_leaking_details():
    """单来源异常不得阻断后续来源或暴露异常正文、URL 密钥。"""
    appended = []

    def broken(url, timeout):
        raise RuntimeError("api_key=top-secret body=private-content")

    def good(url, timeout, max_chars=4000):
        return make_evidence(url)

    result = gather_progressively(
        [
            {
                "source": "weather-api",
                "kind": "json",
                "url": "https://example.com/one?api_key=top-secret",
            },
            {"kind": "text", "url": "https://example.com/two"},
        ],
        deadline=10,
        append_evidence=appended.append,
        clock=lambda: 0.0,
        fetchers={"json": broken, "text": good},
    )

    encoded = json.dumps(result)
    assert result["status"] == "partial"
    assert result["errors"] == [
        {"source": "weather-api", "stage": "fetch", "error": "RuntimeError"}
    ]
    assert appended == [make_evidence("https://example.com/two")]
    assert "top-secret" not in encoded
    assert "private-content" not in encoded


def test_gather_progressively_isolates_malformed_adapter_results():
    """异常适配器返回值应按单来源失败处理，而非中断整轮。"""
    appended = []

    def malformed(url, timeout):
        return None

    def good(url, timeout, max_chars=4000):
        return make_evidence(url)

    result = gather_progressively(
        [
            {"kind": "json", "url": "https://example.com/bad"},
            {"kind": "text", "url": "https://example.com/good"},
        ],
        deadline=10,
        append_evidence=appended.append,
        clock=lambda: 0.0,
        fetchers={"json": malformed, "text": good},
    )

    assert result["status"] == "partial"
    assert result["errors"][0]["source"] == "source-1"
    assert appended == [make_evidence("https://example.com/good")]


def test_gather_progressively_never_appends_after_deadline():
    """来源越过截止时间后不得落库，也不得继续下一来源。"""
    clock = FakeClock()
    calls = []
    appended = []

    def slow(url, timeout):
        calls.append(url)
        clock.advance(6)
        return make_evidence(url)

    result = gather_progressively(
        [
            {"kind": "json", "url": "https://example.com/slow"},
            {"kind": "json", "url": "https://example.com/never"},
        ],
        deadline=5,
        append_evidence=appended.append,
        clock=clock,
        fetchers={"json": slow},
    )

    assert calls == ["https://example.com/slow"]
    assert appended == []
    assert result["evidence"] == []
    assert result["deadline_reached"] is True
    assert result["status"] == "failed"


def test_gather_progressively_isolates_persistence_failure():
    """一次 append_evidence 失败不得阻断后续来源持久化。"""
    appended = []

    def fetcher(url, timeout):
        return make_evidence(url)

    def append(evidence):
        if not appended:
            appended.append("failed-once")
            raise OSError("secret database details")
        appended.append(evidence["url"])

    result = gather_progressively(
        [
            {"kind": "json", "url": "https://example.com/one"},
            {"kind": "json", "url": "https://example.com/two"},
        ],
        deadline=10,
        append_evidence=append,
        clock=lambda: 0.0,
        fetchers={"json": fetcher},
    )

    assert len(result["evidence"]) == 2
    assert result["persisted"] == 1
    assert result["errors"] == [
        {"source": "source-1", "stage": "persist", "error": "OSError"}
    ]
    assert appended == ["failed-once", "https://example.com/two"]
    assert "secret database details" not in json.dumps(result)


def test_gather_progressively_preserves_browser_fallback_with_http_evidence():
    """某来源需浏览器时仍应尝试后续 HTTP 来源并保留已有证据。"""

    def text_fetcher(url, timeout, max_chars=4000):
        return {"fallback": "browser"}

    def json_fetcher(url, timeout):
        return make_evidence(url)

    result = gather_progressively(
        [
            {"kind": "text", "url": "https://example.com/js"},
            {"kind": "json", "url": "https://example.com/api"},
        ],
        deadline=10,
        clock=lambda: 0.0,
        fetchers={"text": text_fetcher, "json": json_fetcher},
    )

    assert result["status"] == "partial"
    assert result["fallback"] == "browser"
    assert result["evidence"] == [make_evidence("https://example.com/api")]


@pytest.mark.parametrize("value", ("nan", "inf", "-inf"))
def test_online_fetch_cli_rejects_non_finite_timeout(
    value, monkeypatch, capsys
):
    """CLI 应在调用抓取编排前拒绝非有限 timeout。"""

    def reject_call(*args, **kwargs):
        raise AssertionError("gather_progressively must not be called")

    monkeypatch.setattr(online_fetch, "gather_progressively", reject_call)

    with pytest.raises(SystemExit) as error:
        online_fetch.main(
            [
                "json",
                "https://example.com/api",
                "--timeout={}".format(value),
            ]
        )

    assert error.value.code == 2
    assert "finite" in capsys.readouterr().err


@responses.activate
def test_online_fetch_cli_appends_successful_evidence_immediately(
    tmp_path, monkeypatch, capsys, public_http
):
    """带 job-id 的 CLI 成功后应将证据写入指定队列。"""
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue(
        {"conv_id": "u1", "mid": 1, "content": "weather"},
        detected_at=100,
    )
    assert db.claim("cursor", 500, 1, 120)
    monkeypatch.setattr(online_fetch.time, "time", lambda: 1)
    url = "https://example.com/weather.json"
    responses.add(
        responses.GET,
        url,
        json={"temperature": 30},
        status=200,
    )

    code = online_fetch.main(
        [
            "json",
            url,
            "--job-id",
            str(job_id),
            "--owner",
            "cursor",
            "--db",
            str(path),
            "--timeout",
            "5",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["status"] == "complete"
    assert db.get(job_id)["evidence"][0]["data"] == {"temperature": 30}


@pytest.mark.parametrize(
    "owner,now_seconds", (("stale", 1), ("cursor", 121))
)
@responses.activate
def test_online_fetch_cli_rejects_unowned_or_expired_evidence(
    owner, now_seconds, tmp_path, monkeypatch, capsys, public_http
):
    """联网结果不得绕过当前 processing owner 的有效租约。"""
    path = tmp_path / "queue.db"
    db = QueueDB(path)
    job_id = db.enqueue({"conv_id": "u1", "mid": 1}, detected_at=100)
    assert db.claim("cursor", 500, 1, 120)
    monkeypatch.setattr(online_fetch.time, "time", lambda: now_seconds)
    url = "https://example.com/data.json"
    responses.add(responses.GET, url, json={"ok": True}, status=200)

    code = online_fetch.main(
        [
            "json", url, "--job-id", str(job_id), "--owner", owner,
            "--db", str(path), "--timeout", "5",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result["errors"][0]["stage"] == "persist"
    assert db.get(job_id)["evidence"] == []


@responses.activate
def test_online_fetch_cli_sanitizes_http_errors(capsys, public_http):
    """CLI 失败输出不得包含 URL 密钥、响应正文或异常详情。"""
    url = "https://example.com/data?api_key=top-secret"
    responses.add(
        responses.GET,
        url,
        body="private-response-body",
        status=500,
    )

    code = online_fetch.main(["json", url, "--timeout", "5"])

    captured = capsys.readouterr()
    assert code == 1
    assert "top-secret" not in captured.out + captured.err
    assert "private-response-body" not in captured.out + captured.err
    assert json.loads(captured.out)["errors"][0]["error"] == "HTTPStatusError"
