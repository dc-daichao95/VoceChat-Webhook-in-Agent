"""有界 HTTP 抓取与结构化内容提取的行为测试。"""

import pytest
import requests
import responses

from scheduler.online import (
    MAX_RESPONSE_BYTES,
    FetchDeadlineExceeded,
    OnlineFetchError,
    ResponseTooLarge,
    UnsafeURLError,
    fetch_text,
    fetch_url,
)

PUBLIC_IP = "93.184.216.34"


def public_resolver(hostname, port):
    """为 HTTP 测试返回固定公网地址。"""
    return [PUBLIC_IP]


def public_peer(response):
    """为无真实 socket 的响应替身返回固定公网 peer。"""
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


class StubResponse:
    """模拟 requests 流式响应，不执行网络访问。"""

    def __init__(
        self,
        body=b"",
        status_code=200,
        headers=None,
        chunks=None,
        before_chunk=None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks if chunks is not None else [body])
        self._before_chunk = before_chunk
        self.closed = False

    def iter_content(self, chunk_size):
        """按预设顺序产出响应块。"""
        assert chunk_size > 0
        for chunk in self._chunks:
            if self._before_chunk is not None:
                self._before_chunk()
            yield chunk

    def close(self):
        """记录响应已关闭。"""
        self.closed = True


class StubSession:
    """记录 HTTP 调用参数并依次返回预设响应。"""

    def __init__(self, response_items):
        self.responses = list(response_items)
        self.calls = []

    def get(self, url, **kwargs):
        """返回下一项响应。"""
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_fetch_url_uses_streaming_and_explicit_connect_read_timeouts():
    """底层请求必须流式读取且显式区分连接与读取超时。"""
    session = StubSession([StubResponse(body=b"ok")])

    result = fetch_url(
        "https://example.com/data",
        timeout=8,
        connect_timeout=3,
        read_timeout=5,
        session=session,
        clock=lambda: 0.0,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    assert result.body == b"ok"
    assert session.calls == [
        (
            "https://example.com/data",
            {
                "allow_redirects": False,
                "stream": True,
                "timeout": (3.0, 5.0),
            },
        )
    ]


@pytest.mark.parametrize("name", ("timeout", "connect_timeout", "read_timeout"))
@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_fetch_url_rejects_non_finite_timeout_inputs(name, value):
    """所有 HTTP 时间输入都必须是有限正数。"""
    session = StubSession([])

    with pytest.raises(ValueError):
        fetch_url(
            "https://example.com/data",
            session=session,
            clock=lambda: 0.0,
            **{name: value},
        )

    assert session.calls == []


@pytest.mark.parametrize(
    "name,value",
    (
        ("timeout", "5"),
        ("timeout", True),
        ("connect_timeout", None),
        ("read_timeout", False),
    ),
)
def test_fetch_url_rejects_non_numeric_timeout_types(name, value):
    """时间参数不得通过隐式转换接受字符串、布尔值或空值。"""
    with pytest.raises(TypeError):
        fetch_url(
            "https://example.com/data",
            session=StubSession([]),
            clock=lambda: 0.0,
            **{name: value},
        )


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_fetch_url_rejects_non_finite_clock_values(value):
    """单调时钟读数异常时不得发起网络请求。"""
    session = StubSession([])

    with pytest.raises(ValueError):
        fetch_url(
            "https://example.com/data",
            session=session,
            clock=lambda: value,
        )

    assert session.calls == []


def test_fetch_url_enforces_total_budget_while_streaming():
    """读取跨过总预算后必须终止，而非只依赖逐次 read timeout。"""
    clock = FakeClock()
    response = StubResponse(
        chunks=[b"late"],
        before_chunk=lambda: clock.advance(2),
    )

    with pytest.raises(FetchDeadlineExceeded):
        fetch_url(
            "https://example.com/slow",
            timeout=1,
            session=StubSession([response]),
            clock=clock,
            resolver=public_resolver,
            peer_getter=public_peer,
        )

    assert response.closed


def test_fetch_url_sanitizes_streaming_network_errors():
    """流式网络异常不得向调用方传播可能含凭据的原始详情。"""

    class BrokenResponse(StubResponse):
        def iter_content(self, chunk_size):
            raise requests.ConnectionError("api_key=top-secret")

    with pytest.raises(OnlineFetchError) as error:
        fetch_url(
            "https://example.com/data?api_key=top-secret",
            session=StubSession([BrokenResponse()]),
            clock=lambda: 0.0,
            resolver=public_resolver,
            peer_getter=public_peer,
        )

    assert type(error.value) is OnlineFetchError
    assert "top-secret" not in str(error.value)
    assert error.value.__cause__ is None


def test_fetch_url_rejects_declared_and_streamed_oversize_bodies():
    """响应头或实际流超过 2 MiB 上限时均应拒绝。"""
    assert MAX_RESPONSE_BYTES == 2 * 1024 * 1024
    declared = StubResponse(
        headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}
    )
    with pytest.raises(ResponseTooLarge):
        fetch_url(
            "https://example.com/declared",
            session=StubSession([declared]),
            clock=lambda: 0.0,
            resolver=public_resolver,
            peer_getter=public_peer,
        )

    streamed = StubResponse(chunks=[b"abc", b"de"])
    with pytest.raises(ResponseTooLarge):
        fetch_url(
            "https://example.com/streamed",
            max_bytes=4,
            session=StubSession([streamed]),
            clock=lambda: 0.0,
            resolver=public_resolver,
            peer_getter=public_peer,
        )


@pytest.mark.parametrize(
    "name,value,error_type",
    (
        ("max_bytes", True, TypeError),
        ("max_bytes", 1.5, TypeError),
        ("max_bytes", 0, ValueError),
        ("max_bytes", MAX_RESPONSE_BYTES + 1, ValueError),
        ("max_redirects", True, TypeError),
        ("max_redirects", 1.5, TypeError),
        ("max_redirects", -1, ValueError),
    ),
)
def test_fetch_url_strictly_validates_integer_limits(
    name, value, error_type
):
    """大小与重定向上限必须使用范围内的真正整数。"""
    session = StubSession([])

    with pytest.raises(error_type):
        fetch_url(
            "https://example.com/data",
            session=session,
            clock=lambda: 0.0,
            **{name: value},
        )

    assert session.calls == []


@responses.activate
@pytest.mark.parametrize(
    "value,error_type",
    (
        (True, TypeError),
        (1.5, TypeError),
        ("100", TypeError),
        (0, ValueError),
        (-1, ValueError),
    ),
)
def test_fetch_text_strictly_validates_max_chars(value, error_type):
    """文本输出上限必须是正整数且不得隐式转换。"""
    url = "https://example.com/page"
    responses.add(responses.GET, url, body="content", status=200)

    with pytest.raises(error_type):
        fetch_text(url, timeout=5, max_chars=value)


@pytest.mark.parametrize(
    "url",
    (
        "ftp://example.com/file",
        "file:///etc/passwd",
        "https://user:password@example.com/private",
        "https://example.com/\nheader",
    ),
)
def test_fetch_url_rejects_unsafe_initial_urls(url):
    """仅允许无凭据、无控制字符的 HTTP(S) URL。"""
    with pytest.raises(UnsafeURLError):
        fetch_url(url, session=StubSession([]), clock=lambda: 0.0)


def test_fetch_url_follows_relative_redirect_and_revalidates_target():
    """重定向应由调用方逐跳处理，并对每个目标重新执行 URL 规则。"""
    first = StubResponse(status_code=302, headers={"Location": "/final"})
    session = StubSession([first, StubResponse(body=b"done")])

    result = fetch_url(
        "https://example.com/start",
        session=session,
        clock=lambda: 0.0,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    assert result.url == "https://example.com/final"
    assert [call[0] for call in session.calls] == [
        "https://example.com/start",
        "https://example.com/final",
    ]
    assert all(call[1]["allow_redirects"] is False for call in session.calls)

    unsafe = StubSession(
        [StubResponse(status_code=302, headers={"Location": "file:///etc"})]
    )
    with pytest.raises(UnsafeURLError):
        fetch_url(
            "https://example.com/start",
            session=unsafe,
            clock=lambda: 0.0,
            resolver=public_resolver,
            peer_getter=public_peer,
        )
    assert len(unsafe.calls) == 1
