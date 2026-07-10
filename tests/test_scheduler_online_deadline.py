"""HTTP 阻塞边界与渐进持久化 deadline 行为测试。"""

import pytest

from scheduler.online import FetchDeadlineExceeded, fetch_url, gather_progressively

PUBLIC_IP = "93.184.216.34"


def public_resolver(hostname, port):
    """为 HTTP deadline 测试返回固定公网地址。"""
    return [PUBLIC_IP]


def public_peer(response):
    """为无 socket 的响应替身返回固定公网 peer。"""
    return PUBLIC_IP


class FakeClock:
    """提供可手动推进的单调时钟。"""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        """推进测试时钟。"""
        self.now += seconds


class ExhaustionDelayResponse:
    """在流迭代结束这一阻塞边界推进时间。"""

    status_code = 200
    headers = {}

    def __init__(self, clock):
        self.clock = clock
        self.closed = False

    def iter_content(self, chunk_size):
        """先返回正文，再在 StopIteration 前跨过 deadline。"""
        yield b"body"
        self.clock.advance(2)

    def close(self):
        """记录响应关闭。"""
        self.closed = True


class StubSession:
    """返回单个预设响应。"""

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, url, **kwargs):
        """返回响应并记录请求。"""
        self.calls += 1
        return self.response


class SimpleResponse:
    """提供重定向与终态响应的最小实现。"""

    def __init__(self, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body

    def iter_content(self, chunk_size):
        """返回单个正文块。"""
        yield self.body

    def close(self):
        """匹配 requests 响应关闭接口。"""


def evidence(url):
    """构造渐进编排所需的完整证据。"""
    return {
        "source": "example.com",
        "url": url,
        "title": "Result",
        "summary": "useful",
        "kind": "text",
        "data": None,
    }


def test_fetch_url_raises_if_stream_exhaustion_crosses_deadline():
    """流结束的阻塞点超期后必须抛错，不能返回已读取正文。"""
    clock = FakeClock()
    response = ExhaustionDelayResponse(clock)

    with pytest.raises(FetchDeadlineExceeded):
        fetch_url(
            "https://example.com/data",
            timeout=1,
            session=StubSession(response),
            clock=clock,
            resolver=public_resolver,
            peer_getter=public_peer,
        )

    assert response.closed


def test_fetch_url_clips_each_redirect_timeout_to_remaining_budget():
    """每一跳的 connect/read timeout 都不得超过剩余总预算。"""
    clock = FakeClock()
    calls = []
    responses = [
        SimpleResponse(302, {"Location": "/final"}),
        SimpleResponse(body=b"done"),
    ]

    class TimedSession:
        def get(self, url, **kwargs):
            calls.append(kwargs["timeout"])
            if len(calls) == 1:
                clock.advance(2)
            return responses.pop(0)

    result = fetch_url(
        "https://example.com/start",
        timeout=5,
        connect_timeout=4,
        read_timeout=4,
        session=TimedSession(),
        clock=clock,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    assert result.body == b"done"
    assert calls == [(4.0, 4.0), (3.0, 3.0)]


def test_gather_marks_fetch_deadline_when_source_raises_after_timeout():
    """来源越时抛错后应标记 fetch deadline，且不得尝试后续来源。"""
    clock = FakeClock()
    calls = []

    def fetcher(url, timeout):
        calls.append(url)
        clock.advance(11)
        raise RuntimeError("private source details")

    result = gather_progressively(
        [
            {"kind": "json", "url": "https://example.com/one"},
            {"kind": "json", "url": "https://example.com/two"},
        ],
        deadline=10,
        clock=clock,
        fetchers={"json": fetcher},
    )

    assert calls == ["https://example.com/one"]
    assert result["attempted"] == 1
    assert result["errors"] == [
        {"source": "source-1", "stage": "fetch", "error": "RuntimeError"}
    ]
    assert result["deadline_reached"] is True
    assert result["deadline_stage"] == "fetch"
    assert result["status"] == "failed"


def test_gather_checks_deadline_immediately_before_persistence():
    """抓取后、持久化前超期时应保留证据但不得调用回调。"""
    appended = []
    clock = FakeClock()
    url = "https://example.com/one"

    class ExpiringFallback:
        """编排读取结果状态后推进时钟，不依赖 clock 调用次数。"""

        def __eq__(self, other):
            clock.advance(11)
            return False

    item = evidence(url)
    item["fallback"] = ExpiringFallback()

    result = gather_progressively(
        [{"kind": "json", "url": url}],
        deadline=10,
        append_evidence=appended.append,
        clock=clock,
        fetchers={"json": lambda *args, **kwargs: item},
    )

    assert appended == []
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["url"] == url
    assert result["persisted"] == 0
    assert result["deadline_reached"] is True
    assert result["deadline_stage"] == "persist"
    assert result["status"] == "partial"


def test_gather_observes_deadline_crossed_by_successful_persistence():
    """append 回调返回后超期应保留持久化结果并停止为 partial。"""
    clock = FakeClock()
    appended = []
    url = "https://example.com/one"

    def append(item):
        appended.append(item)
        clock.advance(11)

    result = gather_progressively(
        [{"kind": "json", "url": url}],
        deadline=10,
        append_evidence=append,
        clock=clock,
        fetchers={"json": lambda *args, **kwargs: evidence(url)},
    )

    assert appended == [evidence(url)]
    assert result["persisted"] == 1
    assert result["deadline_reached"] is True
    assert result["deadline_stage"] == "persist"
    assert result["status"] == "partial"


def test_gather_reports_persistence_failure_and_crossed_deadline():
    """持久化失败同时越过 deadline 时两种状态都必须可观测。"""
    clock = FakeClock()
    url = "https://example.com/one"

    def append(item):
        clock.advance(11)
        raise OSError("private database details")

    result = gather_progressively(
        [{"kind": "json", "url": url}],
        deadline=10,
        append_evidence=append,
        clock=clock,
        fetchers={"json": lambda *args, **kwargs: evidence(url)},
    )

    assert result["evidence"] == [evidence(url)]
    assert result["persisted"] == 0
    assert result["errors"] == [
        {"source": "source-1", "stage": "persist", "error": "OSError"}
    ]
    assert result["deadline_reached"] is True
    assert result["deadline_stage"] == "persist"
    assert result["status"] == "partial"
