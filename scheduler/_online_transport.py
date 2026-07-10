"""通过 requests/urllib3 把每次连接固定到预先批准的 IP。"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Callable, Optional, Tuple
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

Connector = Callable[..., socket.socket]
SocketFactory = Callable[[int, int], socket.socket]


def default_connector(
    address: Tuple[str, int],
    timeout: float,
    *,
    source_address=None,
    socket_options=None,
    socket_factory: SocketFactory = socket.socket,
) -> socket.socket:
    """直接连接 IP 字面量，不调用任何 hostname 解析。"""
    version = ipaddress.ip_address(address[0]).version
    family = socket.AF_INET6 if version == 6 else socket.AF_INET
    active_socket = socket_factory(family, socket.SOCK_STREAM)
    try:
        if isinstance(timeout, (int, float)):
            active_socket.settimeout(timeout)
        for option in socket_options or ():
            active_socket.setsockopt(*option)
        if source_address is not None:
            active_socket.bind(source_address)
        active_socket.connect(address)
        return active_socket
    except BaseException:
        active_socket.close()
        raise


def _open_pinned_socket(connection) -> socket.socket:
    try:
        return connection._pinned_connector(
            (connection._pinned_ip, connection.port),
            connection.timeout,
            source_address=connection.source_address,
            socket_options=connection.socket_options,
        )
    except requests.exceptions.SSLError:
        raise
    except socket.timeout as error:
        raise ConnectTimeoutError(
            connection,
            "connection to preapproved address timed out",
        ) from error
    except OSError as error:
        raise NewConnectionError(
            connection, "connection to preapproved address failed"
        ) from error


class PinnedHTTPConnection(HTTPConnection):
    """保留 origin host，但只向指定 IP 建立 TCP 连接。"""

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        *,
        pinned_ip: str,
        connector: Connector = default_connector,
        **kwargs: Any,
    ) -> None:
        self._pinned_ip = pinned_ip
        self._pinned_connector = connector
        super().__init__(host, port, **kwargs)

    def _new_conn(self) -> socket.socket:
        return _open_pinned_socket(self)


class PinnedHTTPSConnection(HTTPSConnection):
    """固定 TCP IP，同时由 urllib3 按 origin 执行 SNI 与证书校验。"""

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        *,
        pinned_ip: str,
        connector: Connector = default_connector,
        **kwargs: Any,
    ) -> None:
        self._pinned_ip = pinned_ip
        self._pinned_connector = connector
        super().__init__(host, port, **kwargs)

    def _new_conn(self) -> socket.socket:
        return _open_pinned_socket(self)


class PinnedHTTPConnectionPool(HTTPConnectionPool):
    """为 HTTP 请求创建固定 IP 连接。"""

    ConnectionCls = PinnedHTTPConnection


class PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    """为 HTTPS 请求创建固定 IP 且保留 origin TLS 身份的连接。"""

    ConnectionCls = PinnedHTTPSConnection


class PinnedHTTPAdapter(HTTPAdapter):
    """每个实例只服务一个 origin 的一个预批准地址。"""

    def __init__(self, pinned_ip: str, connector: Connector) -> None:
        self.pinned_ip = pinned_ip
        self.connector = connector
        self._active_pool = None
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(
        self, request, verify, proxies=None, cert=None
    ):
        """创建 origin-host pool，并把底层连接固定到批准 IP。"""
        if proxies and any(proxies.values()):
            raise requests.exceptions.ProxyError("proxies are disabled")
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request, True, cert
        )
        scheme = host_params["scheme"]
        host = host_params["host"]
        port = host_params["port"]
        pool_kwargs.update(
            pinned_ip=self.pinned_ip,
            connector=self.connector,
        )
        if scheme == "https":
            pool_kwargs.update(
                assert_hostname=host,
                server_hostname=host,
            )
            pool_class = PinnedHTTPSConnectionPool
        elif scheme == "http":
            pool_kwargs = {
                "pinned_ip": self.pinned_ip,
                "connector": self.connector,
            }
            pool_class = PinnedHTTPConnectionPool
        else:
            raise requests.exceptions.InvalidURL("unsupported URL scheme")
        self._active_pool = pool_class(host, port, **pool_kwargs)
        return self._active_pool

    def close(self) -> None:
        """关闭本次请求创建的独立连接池。"""
        if self._active_pool is not None:
            self._active_pool.close()
            self._active_pool = None
        super().close()


def send_pinned(
    session: requests.Session,
    url: str,
    pinned_ip: str,
    timeout: Tuple[float, float],
    connector: Connector,
):
    """发送单次固定 IP GET；始终启用 TLS 验证并禁用代理。"""
    request = requests.Request(
        "GET", url, headers={"Host": urlsplit(url).netloc}
    )
    prepared = session.prepare_request(request)
    adapter = PinnedHTTPAdapter(pinned_ip, connector)
    try:
        response = adapter.send(
            prepared,
            stream=True,
            timeout=timeout,
            verify=True,
            cert=None,
            proxies={},
        )
    except BaseException:
        adapter.close()
        raise
    response._pinned_adapter = adapter
    return response


def close_pinned_response(response) -> None:
    """关闭响应及其请求专用 adapter/pool。"""
    try:
        response.close()
    finally:
        adapter = getattr(response, "_pinned_adapter", None)
        if adapter is not None:
            adapter.close()
