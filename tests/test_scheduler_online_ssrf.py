"""HTTP 快路径公网目标、逐跳解析与连接 peer 复验测试。"""

import pytest

from scheduler.online import UnsafeURLError, fetch_url


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


class StubResponse:
    """提供状态、peer 与流式正文的最小响应。"""

    def __init__(self, peer=PUBLIC_V4, status=200, headers=None, body=b"ok"):
        self.peer = peer
        self.status_code = status
        self.headers = headers or {}
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size):
        """返回单个正文块。"""
        yield self.body

    def close(self):
        """记录响应已关闭。"""
        self.closed = True


class StubSession:
    """记录逐跳请求，并暴露环境代理开关。"""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        """返回下一响应。"""
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def resolver_for(mapping):
    """构造按 hostname 返回固定地址集合的确定性 resolver。"""

    def resolve(hostname, port):
        assert port in (80, 443)
        return mapping[hostname]

    return resolve


def response_peer(response):
    """从测试响应读取显式 peer。"""
    return response.peer


@pytest.mark.parametrize(
    "url,addresses",
    (
        ("http://localhost/data", ["127.0.0.1"]),
        ("http://127.0.0.1/data", None),
        ("http://10.0.0.1/data", None),
        ("http://169.254.1.1/data", None),
        ("http://224.0.0.1/data", None),
        ("http://0.0.0.0/data", None),
        ("http://240.0.0.1/data", None),
        ("http://[::1]/data", None),
        ("http://[fe80::1]/data", None),
        ("http://[ff02::1]/data", None),
        ("http://[::]/data", None),
        ("http://[fd00::1]/data", None),
    ),
)
def test_fetch_url_rejects_every_non_public_address_class(url, addresses):
    """直连 IP 与解析结果中的所有非公网地址类别都必须拒绝。"""
    session = StubSession()
    resolver = resolver_for({"localhost": addresses}) if addresses else None

    with pytest.raises(UnsafeURLError):
        fetch_url(
            url,
            session=session,
            resolver=resolver,
            peer_getter=response_peer,
            clock=lambda: 0.0,
        )

    assert session.calls == []


def test_fetch_url_rejects_domain_when_any_dns_answer_is_not_public():
    """混合 DNS 结果不能只挑公网地址后继续请求。"""
    session = StubSession()

    with pytest.raises(UnsafeURLError):
        fetch_url(
            "https://mixed.example/data",
            session=session,
            resolver=resolver_for(
                {"mixed.example": [PUBLIC_V4, "127.0.0.1"]}
            ),
            peer_getter=response_peer,
            clock=lambda: 0.0,
        )

    assert session.calls == []


@pytest.mark.parametrize(
    "url,peer",
    (
        ("http://93.184.216.34/data", PUBLIC_V4),
        ("https://[2606:4700:4700::1111]/data", PUBLIC_V6),
    ),
)
def test_fetch_url_allows_public_ip_literals_and_disables_environment_proxy(
    url, peer
):
    """公网字面量可直连，但 Session 必须禁用环境代理。"""
    session = StubSession([StubResponse(peer=peer)])

    result = fetch_url(
        url,
        session=session,
        peer_getter=response_peer,
        clock=lambda: 0.0,
    )

    assert result.body == b"ok"
    assert session.trust_env is False
    assert len(session.calls) == 1


def test_fetch_url_accepts_peer_from_preapproved_dns_set():
    """响应连接 peer 必须属于请求前批准的完整公网 DNS 集合。"""
    session = StubSession([StubResponse(peer=PUBLIC_V4)])

    result = fetch_url(
        "https://public.example/data",
        session=session,
        resolver=resolver_for(
            {"public.example": [PUBLIC_V4, "142.250.72.14"]}
        ),
        peer_getter=response_peer,
        clock=lambda: 0.0,
    )

    assert result.body == b"ok"
    assert session.trust_env is False


@pytest.mark.parametrize("peer", ("1.1.1.1", "127.0.0.1", None))
def test_fetch_url_fails_closed_for_rebound_or_unknown_peer(peer):
    """连接 peer 不在批准集合或无法取得时，正文不得返回调用方。"""
    response = StubResponse(peer=peer, body=b"private response")
    session = StubSession([response])

    with pytest.raises(UnsafeURLError):
        fetch_url(
            "https://public.example/data",
            session=session,
            resolver=resolver_for({"public.example": [PUBLIC_V4]}),
            peer_getter=response_peer,
            clock=lambda: 0.0,
        )

    assert response.closed


def test_fetch_url_default_peer_lookup_fails_closed_for_test_double():
    """生产默认 peer 提取器不能把无连接信息的测试替身当成安全响应。"""
    response = StubResponse()

    with pytest.raises(UnsafeURLError):
        fetch_url(
            "https://public.example/data",
            session=StubSession([response]),
            resolver=resolver_for({"public.example": [PUBLIC_V4]}),
            clock=lambda: 0.0,
        )

    assert response.closed


def test_fetch_url_revalidates_redirect_target_before_second_request():
    """重定向到私网目标时不得发出第二跳请求。"""
    first = StubResponse(
        peer=PUBLIC_V4,
        status=302,
        headers={"Location": "https://127.0.0.1/private"},
    )
    session = StubSession([first])

    with pytest.raises(UnsafeURLError):
        fetch_url(
            "https://public.example/start",
            session=session,
            resolver=resolver_for({"public.example": [PUBLIC_V4]}),
            peer_getter=response_peer,
            clock=lambda: 0.0,
        )

    assert len(session.calls) == 1


def test_fetch_url_rejects_https_to_http_redirect_even_for_public_target():
    """HTTPS 响应不得降级重定向到公网 HTTP。"""
    first = StubResponse(
        peer=PUBLIC_V4,
        status=302,
        headers={"Location": "http://next.example/final"},
    )
    session = StubSession([first])

    with pytest.raises(UnsafeURLError):
        fetch_url(
            "https://public.example/start",
            session=session,
            resolver=resolver_for(
                {
                    "public.example": [PUBLIC_V4],
                    "next.example": ["142.250.72.14"],
                }
            ),
            peer_getter=response_peer,
            clock=lambda: 0.0,
        )

    assert len(session.calls) == 1
