"""实现有界 HTTP 读取、逐跳公网解析及连接 peer 复验。"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Optional,
    Tuple,
)
from urllib.parse import urljoin, urlsplit

import requests
from urllib3.exceptions import SSLError as Urllib3SSLError

from scheduler._online_support import (
    finite_number,
    nonnegative_int,
    normalize_url,
    positive_finite,
    positive_int,
    public_url,
)
from scheduler._online_transport import (
    Connector,
    close_pinned_response,
    default_connector,
    send_pinned,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 5.0
DEFAULT_SOURCE_TIMEOUT = 8.0
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
Resolver = Callable[[str, int], Iterable[str]]
PeerGetter = Callable[[Any], Optional[str]]


class OnlineFetchError(RuntimeError):
    """联网快路径无法安全取得可用内容。"""


class UnsafeURLError(OnlineFetchError):
    """URL、解析地址或实际连接 peer 不符合公网安全规则。"""


class FetchDeadlineExceeded(OnlineFetchError):
    """HTTP 请求或流式读取超过调用方给出的总预算。"""


class ResponseTooLarge(OnlineFetchError):
    """响应声明或实际读取的正文超过允许上限。"""


class HTTPStatusError(OnlineFetchError):
    """HTTP 终态响应不是成功状态，且不携带正文或 URL。"""

    def __init__(self, status_code: int) -> None:
        """仅保留状态码，避免异常文本泄漏 URL 凭据或正文。"""
        self.status_code = status_code
        super().__init__("HTTP status {}".format(status_code))


@dataclass(frozen=True)
class FetchedContent:
    """保存经过大小、URL、peer 与时限校验的响应正文和元数据。"""

    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes


def header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """大小写无关地读取响应头。"""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return None


def _validate_url(url: str) -> str:
    try:
        return normalize_url(url)
    except (TypeError, ValueError):
        raise UnsafeURLError("unsafe URL") from None


def _clock_now(clock: Callable[[], float]) -> float:
    if not callable(clock):
        raise TypeError("clock must be callable")
    return finite_number("clock result", clock())


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    remaining = finite_number(
        "remaining budget",
        finite_number("deadline", deadline) - _clock_now(clock),
    )
    if remaining <= 0:
        raise FetchDeadlineExceeded("HTTP fetch exceeded total budget")
    return remaining


def _is_public(address: ipaddress._BaseAddress) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public(mapped)
    blocked = (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )
    return bool(address.is_global and not blocked)


def default_resolver(hostname: str, port: int) -> Iterable[str]:
    """使用系统 DNS 返回 TCP 连接候选地址。"""
    records = socket.getaddrinfo(
        hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return tuple(record[4][0] for record in records)


def _resolved_addresses(
    hostname: str, port: int, resolver: Resolver
) -> Tuple[str, ...]:
    try:
        values = tuple(resolver(hostname, port))
    # resolver 是可注入边界；任何实现细节都不能进入安全错误。
    except Exception:
        raise UnsafeURLError("unable to resolve public host") from None
    try:
        addresses = tuple(ipaddress.ip_address(value) for value in values)
    except (TypeError, ValueError):
        raise UnsafeURLError("resolver returned an invalid address") from None
    if not addresses or any(not _is_public(item) for item in addresses):
        raise UnsafeURLError("target does not resolve only to public addresses")
    return tuple(dict.fromkeys(str(item) for item in addresses))


def approved_addresses(url: str, resolver: Resolver) -> Tuple[str, ...]:
    """返回 URL 每个候选都为公网时的规范化批准地址集合。"""
    parts = urlsplit(url)
    hostname = parts.hostname
    if hostname is None:
        raise UnsafeURLError("unsafe URL")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return _resolved_addresses(hostname, port, resolver)
    if not _is_public(literal):
        raise UnsafeURLError("target address is not public")
    return (str(literal),)


def default_peer_getter(response: Any) -> Optional[str]:
    """从 requests/urllib3 响应中提取实际 TCP 连接 peer。"""
    raw = getattr(response, "raw", None)
    candidates = (
        getattr(getattr(raw, "_connection", None), "sock", None),
        getattr(getattr(raw, "connection", None), "sock", None),
        getattr(
            getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None),
            "_sock",
            None,
        ),
    )
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return str(candidate.getpeername()[0])
        except (AttributeError, OSError, TypeError):
            continue
    return None


def verify_response_peer(
    response: Any,
    approved: Collection[str],
    peer_getter: PeerGetter,
) -> None:
    """确认真实连接 peer 仍属于请求前批准的公网集合。"""
    try:
        peer = peer_getter(response)
    # peer 适配器是安全边界；失败必须净化并安全关闭。
    except Exception:
        raise UnsafeURLError("unable to verify response peer") from None
    try:
        normalized = str(ipaddress.ip_address(peer)) if peer else None
    except ValueError:
        normalized = None
    if normalized is None or normalized not in approved:
        raise UnsafeURLError("response peer was not preapproved")


def _contains_tls_error(error: BaseException) -> bool:
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(
            current, (requests.exceptions.SSLError, Urllib3SSLError)
        ):
            return True
        for name in ("__cause__", "__context__", "reason"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
        pending.extend(
            item for item in current.args if isinstance(item, BaseException)
        )
    return False


def _request(
    session: requests.Session,
    url: str,
    approved: Tuple[str, ...],
    deadline: float,
    clock: Callable[[], float],
    connect_timeout: float,
    read_timeout: float,
    connector: Connector,
):
    if not isinstance(session, requests.Session):
        remaining = _remaining(deadline, clock)
        timeout = (
            min(connect_timeout, remaining),
            min(read_timeout, remaining),
        )
        return _request_from_test_double(session, url, timeout)
    saw_connect_timeout = False
    for address in approved:
        remaining = _remaining(deadline, clock)
        timeout = (
            min(connect_timeout, remaining),
            min(read_timeout, remaining),
        )
        try:
            return send_pinned(session, url, address, timeout, connector)
        except requests.exceptions.SSLError:
            raise OnlineFetchError(
                "HTTPS certificate verification failed"
            ) from None
        except requests.exceptions.ConnectTimeout:
            saw_connect_timeout = True
        except requests.exceptions.ConnectionError as error:
            if _contains_tls_error(error):
                raise OnlineFetchError(
                    "HTTPS certificate verification failed"
                ) from None
            continue
        except requests.exceptions.Timeout:
            raise FetchDeadlineExceeded("HTTP request timed out") from None
        except requests.exceptions.RequestException as error:
            raise OnlineFetchError(
                "HTTP request failed: {}".format(type(error).__name__)
            ) from None
    if saw_connect_timeout:
        raise FetchDeadlineExceeded("HTTP connection timed out")
    raise OnlineFetchError("HTTP request failed: ConnectionError")


def _request_from_test_double(
    session,
    url: str,
    timeout: Tuple[float, float],
):
    try:
        return session.get(url, allow_redirects=False, stream=True, timeout=timeout)
    except requests.exceptions.Timeout:
        raise FetchDeadlineExceeded("HTTP request timed out") from None
    except requests.exceptions.RequestException as error:
        raise OnlineFetchError(
            "HTTP request failed: {}".format(type(error).__name__)
        ) from None


def _response_chunks(
    response: Any,
    deadline: float,
    clock: Callable[[], float],
):
    _remaining(deadline, clock)
    try:
        iterator = iter(response.iter_content(chunk_size=64 * 1024))
    except requests.exceptions.Timeout:
        raise FetchDeadlineExceeded("HTTP response timed out") from None
    except requests.exceptions.RequestException as error:
        raise OnlineFetchError(
            "HTTP response failed: {}".format(type(error).__name__)
        ) from None
    _remaining(deadline, clock)
    while True:
        _remaining(deadline, clock)
        try:
            chunk = next(iterator)
        except StopIteration:
            _remaining(deadline, clock)
            return
        except requests.exceptions.Timeout:
            raise FetchDeadlineExceeded("HTTP response timed out") from None
        except requests.exceptions.RequestException as error:
            raise OnlineFetchError(
                "HTTP response failed: {}".format(type(error).__name__)
            ) from None
        _remaining(deadline, clock)
        yield chunk


def read_body(
    response: Any,
    max_bytes: int,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    """读取 requests 已解压的流，并对实际正文实施硬大小上限。"""
    declared = header(response.headers, "Content-Length")
    if (
        declared is not None
        and declared.strip().isdigit()
        and int(declared) > max_bytes
    ):
        raise ResponseTooLarge("response exceeds size limit")
    chunks = []
    total = 0
    for chunk in _response_chunks(response, deadline, clock):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge("response exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_options(
    timeout: float,
    connect_timeout: float,
    read_timeout: float,
    max_bytes: int,
    max_redirects: int,
) -> None:
    positive_finite("timeout", timeout)
    positive_finite("connect_timeout", connect_timeout)
    positive_finite("read_timeout", read_timeout)
    positive_int("max_bytes", max_bytes, maximum=MAX_RESPONSE_BYTES)
    nonnegative_int("max_redirects", max_redirects)


def _redirect_target(current_url: str, location: str) -> str:
    target = _validate_url(urljoin(current_url, location))
    if (
        urlsplit(current_url).scheme == "https"
        and urlsplit(target).scheme != "https"
    ):
        raise UnsafeURLError("HTTPS redirect downgrade is forbidden")
    return target


def _fetch_loop(
    initial_url: str,
    deadline: float,
    active_session: requests.Session,
    resolver: Resolver,
    peer_getter: PeerGetter,
    connect_timeout: float,
    read_timeout: float,
    max_bytes: int,
    max_redirects: int,
    clock: Callable[[], float],
    connector: Connector,
) -> FetchedContent:
    current_url = initial_url
    for redirect_count in range(max_redirects + 1):
        approved = approved_addresses(current_url, resolver)
        response = _request(
            active_session,
            current_url,
            approved,
            deadline,
            clock,
            connect_timeout,
            read_timeout,
            connector,
        )
        try:
            _remaining(deadline, clock)
            verify_response_peer(response, approved, peer_getter)
            status = int(response.status_code)
            if status not in _REDIRECT_STATUSES:
                if not 200 <= status < 300:
                    raise HTTPStatusError(status)
                body = read_body(response, max_bytes, deadline, clock)
                return FetchedContent(
                    public_url(current_url), status, dict(response.headers), body
                )
            if redirect_count >= max_redirects:
                raise OnlineFetchError("too many HTTP redirects")
            location = header(response.headers, "Location")
            if not location:
                raise OnlineFetchError("redirect has no location")
            current_url = _redirect_target(current_url, location)
        finally:
            close_pinned_response(response)
    raise OnlineFetchError("too many HTTP redirects")


def fetch_url(
    url: str,
    timeout: float = DEFAULT_SOURCE_TIMEOUT,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    session: Optional[requests.Session] = None,
    clock: Callable[[], float] = time.monotonic,
    resolver: Optional[Resolver] = None,
    peer_getter: Optional[PeerGetter] = None,
    connector: Optional[Connector] = None,
) -> FetchedContent:
    """逐跳解析公网地址、复验连接 peer，并有界读取响应。"""
    _validate_options(
        timeout, connect_timeout, read_timeout, max_bytes, max_redirects
    )
    current_url = _validate_url(url)
    deadline = finite_number("deadline", _clock_now(clock) + float(timeout))
    active_session = session or requests.Session()
    owns_session = session is None
    active_session.trust_env = False
    try:
        return _fetch_loop(
            current_url,
            deadline,
            active_session,
            resolver or default_resolver,
            peer_getter or default_peer_getter,
            connect_timeout,
            read_timeout,
            max_bytes,
            max_redirects,
            clock,
            connector or default_connector,
        )
    finally:
        if owns_session:
            active_session.close()
