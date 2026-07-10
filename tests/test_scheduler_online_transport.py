"""固定 IP HTTPAdapter、地址轮换及 TLS origin 身份测试。"""

import socket
import ssl
import threading

import pytest
import requests

from scheduler._online_transport import PinnedHTTPAdapter, default_connector
from scheduler.online import OnlineFetchError, fetch_url

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"
PUBLIC_ALT = "142.250.72.14"


def resolver_for(mapping):
    """构造按 hostname 返回固定地址集合的确定性 resolver。"""

    def resolve(hostname, port):
        return mapping[hostname]

    return resolve


def http_response(status="200 OK", headers=(), body=b"ok"):
    """构造供 socketpair 服务端返回的最小 HTTP 响应。"""
    lines = ["HTTP/1.1 {}".format(status), "Content-Length: {}".format(len(body))]
    lines.extend(headers)
    lines.extend(("Connection: close", "", ""))
    return "\r\n".join(lines).encode("ascii") + body


class SocketDialer:
    """记录目标地址，并把批准 IP 的连接转接到内存 HTTP 服务端。"""

    def __init__(self, responses=(), fail=(), timeouts=()):
        self.responses = list(responses or (http_response(),))
        self.fail = frozenset(fail)
        self.timeouts = frozenset(timeouts)
        self.calls = []
        self.requests = []
        self.threads = []

    def __call__(
        self, address, timeout, source_address=None, socket_options=None
    ):
        self.calls.append(address)
        if address[0] in self.timeouts:
            raise socket.timeout("simulated connect timeout")
        if address[0] in self.fail:
            raise OSError("simulated connect failure")
        client, server = socket.socketpair()
        if isinstance(timeout, (int, float)):
            client.settimeout(timeout)
        response = self.responses.pop(0)
        thread = threading.Thread(
            target=self._serve, args=(server, response), daemon=True
        )
        thread.start()
        self.threads.append(thread)
        return client

    def _serve(self, server, response):
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                data += server.recv(4096)
            self.requests.append(data)
            server.sendall(response)
        finally:
            server.close()

    def wait(self):
        """等待所有内存服务端退出。"""
        for thread in self.threads:
            thread.join(timeout=2)


def test_fetch_url_dials_preapproved_ip_without_second_hostname_resolution():
    """预解析后必须固定拨号；后续 DNS 即使变私网也不得被查询或连接。"""
    resolver_calls = []

    def rebinding_resolver(hostname, port):
        resolver_calls.append((hostname, port))
        return [PUBLIC_V4] if len(resolver_calls) == 1 else ["127.0.0.1"]

    dialer = SocketDialer()
    result = fetch_url(
        "http://public.example/data?value=1",
        resolver=rebinding_resolver,
        connector=dialer,
        peer_getter=lambda response: PUBLIC_V4,
        clock=lambda: 0.0,
    )
    dialer.wait()

    assert result.body == b"ok"
    assert resolver_calls == [("public.example", 80)]
    assert dialer.calls == [(PUBLIC_V4, 80)]
    assert b"Host: public.example\r\n" in dialer.requests[0]


def test_fetch_url_rotates_only_across_preapproved_addresses():
    """首个公网地址连接失败时，按 resolver 顺序尝试下一个批准地址。"""
    dialer = SocketDialer(fail=(PUBLIC_V4,))

    result = fetch_url(
        "http://public.example/data",
        resolver=resolver_for(
            {"public.example": [PUBLIC_V4, PUBLIC_ALT]}
        ),
        connector=dialer,
        peer_getter=lambda response: PUBLIC_ALT,
        clock=lambda: 0.0,
    )
    dialer.wait()

    assert result.body == b"ok"
    assert dialer.calls == [(PUBLIC_V4, 80), (PUBLIC_ALT, 80)]


def test_fetch_url_tries_each_preapproved_address_once_then_fails():
    """全部批准地址连接失败后应净化失败，且不得拨号集合外地址。"""
    dialer = SocketDialer(fail=(PUBLIC_V4, PUBLIC_ALT))

    with pytest.raises(OnlineFetchError) as error:
        fetch_url(
            "http://public.example/data",
            resolver=resolver_for(
                {"public.example": [PUBLIC_V4, PUBLIC_ALT]}
            ),
            connector=dialer,
            peer_getter=lambda response: PUBLIC_V4,
            clock=lambda: 0.0,
        )

    assert dialer.calls == [(PUBLIC_V4, 80), (PUBLIC_ALT, 80)]
    assert "simulated" not in str(error.value)


def test_fetch_url_rotates_after_preapproved_address_connect_timeout():
    """单个批准 IP 连接超时应继续下一个，且仍受总预算约束。"""
    dialer = SocketDialer(timeouts=(PUBLIC_V4,))

    result = fetch_url(
        "http://public.example/data",
        resolver=resolver_for(
            {"public.example": [PUBLIC_V4, PUBLIC_ALT]}
        ),
        connector=dialer,
        peer_getter=lambda response: PUBLIC_ALT,
        clock=lambda: 0.0,
    )
    dialer.wait()

    assert result.body == b"ok"
    assert dialer.calls == [(PUBLIC_V4, 80), (PUBLIC_ALT, 80)]


def test_fetch_url_pins_ipv6_address_without_hostname_dial():
    """IPv6 DNS 结果也必须把规范化字面量直接交给 connector。"""
    dialer = SocketDialer()

    result = fetch_url(
        "http://public.example/data",
        resolver=resolver_for({"public.example": [PUBLIC_V6]}),
        connector=dialer,
        peer_getter=lambda response: PUBLIC_V6,
        clock=lambda: 0.0,
    )
    dialer.wait()

    assert result.body == b"ok"
    assert dialer.calls == [(PUBLIC_V6, 80)]


def test_fetch_url_resolves_and_pins_each_redirect_hop():
    """每个重定向主机都应独立预解析并只拨号本跳批准 IP。"""
    first = http_response(
        "302 Found", ("Location: http://next.example/final",), b""
    )
    dialer = SocketDialer((first, http_response()))
    resolver = resolver_for(
        {
            "public.example": [PUBLIC_V4],
            "next.example": [PUBLIC_ALT],
        }
    )

    result = fetch_url(
        "http://public.example/start",
        resolver=resolver,
        connector=dialer,
        peer_getter=lambda response: dialer.calls[-1][0],
        clock=lambda: 0.0,
    )
    dialer.wait()

    assert result.url == "http://next.example/final"
    assert dialer.calls == [(PUBLIC_V4, 80), (PUBLIC_ALT, 80)]
    assert b"Host: public.example\r\n" in dialer.requests[0]
    assert b"Host: next.example\r\n" in dialer.requests[1]


def test_https_pinned_pool_preserves_sni_and_certificate_hostname():
    """固定 TCP IP 时 TLS SNI/hostname 仍须使用 origin 且强制验证证书。"""
    prepared = requests.Request(
        "GET", "https://secure.example/data"
    ).prepare()
    adapter = PinnedHTTPAdapter(PUBLIC_V4, SocketDialer())

    pool = adapter.get_connection_with_tls_context(
        prepared, verify=False, proxies={}, cert=None
    )
    connection = pool._new_conn()

    assert pool.host == "secure.example"
    assert connection.host == "secure.example"
    assert connection.server_hostname == "secure.example"
    assert connection.assert_hostname == "secure.example"
    assert connection._pinned_ip == PUBLIC_V4
    assert connection.cert_reqs == "CERT_REQUIRED"
    assert connection.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert connection.ssl_context.check_hostname is True
    adapter.close()


def test_https_tls_failure_does_not_rotate_to_another_address():
    """证书/TLS 身份失败不是地址可用性失败，不得降级轮换规避验证。"""

    class TLSFailureDialer:
        def __init__(self):
            self.calls = []

        def __call__(
            self, address, timeout, source_address=None, socket_options=None
        ):
            self.calls.append(address)
            raise requests.exceptions.SSLError("certificate mismatch")

    dialer = TLSFailureDialer()

    with pytest.raises(OnlineFetchError):
        fetch_url(
            "https://secure.example/data",
            resolver=resolver_for(
                {"secure.example": [PUBLIC_V4, PUBLIC_ALT]}
            ),
            connector=dialer,
            peer_getter=lambda response: PUBLIC_V4,
            clock=lambda: 0.0,
        )

    assert dialer.calls == [(PUBLIC_V4, 443)]


@pytest.mark.parametrize(
    "address,family",
    (
        ((PUBLIC_V4, 443), socket.AF_INET),
        ((PUBLIC_V6, 443), socket.AF_INET6),
    ),
)
def test_default_connector_uses_numeric_socket_without_dns(address, family):
    """生产 connector 应直接创建对应地址族 socket，不调用 hostname 解析。"""

    class RecordingSocket:
        def __init__(self):
            self.connected = None
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

        def setsockopt(self, level, option, value):
            pass

        def connect(self, target):
            self.connected = target

        def close(self):
            pass

    created = []

    def socket_factory(actual_family, socket_type):
        assert actual_family == family
        assert socket_type == socket.SOCK_STREAM
        created.append(RecordingSocket())
        return created[-1]

    result = default_connector(
        address,
        3.0,
        socket_options=(),
        socket_factory=socket_factory,
    )

    assert result is created[0]
    assert result.timeout == 3.0
    assert result.connected == address
