"""提供有总预算的 HTTP 快路径，并按来源渐进保存结构化证据。"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from scheduler._online_http import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_SOURCE_TIMEOUT,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    FetchDeadlineExceeded,
    FetchedContent,
    HTTPStatusError,
    OnlineFetchError,
    PeerGetter,
    Resolver,
    ResponseTooLarge,
    UnsafeURLError,
    fetch_url,
    header,
)

from scheduler._online_support import (
    analyze_html,
    decode_text,
    finite_number,
    has_browser_markers,
    positive_finite,
    positive_int,
    safe_source_label,
    strict_json_loads,
)

__all__ = (
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_READ_TIMEOUT",
    "DEFAULT_SOURCE_TIMEOUT",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "FetchDeadlineExceeded",
    "FetchedContent",
    "HTTPStatusError",
    "OnlineFetchError",
    "ResponseTooLarge",
    "UnsafeURLError",
    "classify_response",
    "fetch_json",
    "fetch_text",
    "fetch_url",
    "gather_progressively",
)
Fetcher = Callable[..., Dict[str, Any]]
Fetchers = Mapping[str, Fetcher]


def _clock_now(clock: Callable[[], float]) -> float:
    if not callable(clock):
        raise TypeError("clock must be callable")
    return finite_number("clock result", clock())


def _budget_remaining(
    deadline: float, clock: Callable[[], float]
) -> float:
    return finite_number(
        "remaining budget", finite_number("deadline", deadline) - _clock_now(clock)
    )


def _deadline_crossed(
    deadline: float, clock: Callable[[], float]
) -> bool:
    return _budget_remaining(deadline, clock) <= 0


def classify_response(response) -> str:
    """按 Content-Type 与有限正文嗅探分类为 json/html/text/binary。"""
    content_type = (header(response.headers, "Content-Type") or "").lower()
    body = getattr(response, "body", getattr(response, "content", b""))
    prefix = bytes(body[:256]).lstrip().lower()
    html_prefixes = (
        b"<!doctype html", b"<html", b"<head", b"<body", b"<title",
        b"<meta", b"<script", b"<div",
    )
    if b"\x00" in prefix:
        return "binary"
    if "html" in content_type or prefix.startswith(html_prefixes):
        return "html"
    if "json" in content_type:
        return "json"
    if content_type.startswith("text/") or "xml" in content_type:
        return "text"
    if prefix.startswith((b"{", b"[")):
        return "json"
    if content_type:
        return "binary"
    return "text"


def _default_title(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else (urlsplit(url).hostname or "unknown")


def _evidence(
    content: FetchedContent,
    title: str,
    summary: str,
    kind: str,
    data: Any,
) -> Dict[str, Any]:
    return {
        "source": urlsplit(content.url).hostname or "unknown", "url": content.url,
        "title": title, "summary": summary, "kind": kind, "data": data,
    }


def fetch_json(
    url: str,
    timeout: float = DEFAULT_SOURCE_TIMEOUT,
    *,
    resolver: Optional[Resolver] = None,
    peer_getter: Optional[PeerGetter] = None,
) -> Dict[str, Any]:
    """取得有界 JSON，并返回可持久化的统一证据结构。"""
    content = fetch_url(
        url, timeout=timeout, resolver=resolver, peer_getter=peer_getter
    )
    response_kind = classify_response(content)
    if response_kind == "binary":
        raise OnlineFetchError("binary response is not supported")
    value = decode_text(content.body, content.headers)
    if response_kind == "html" or has_browser_markers(value):
        return {"fallback": "browser"}
    try:
        data = strict_json_loads(content.body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise OnlineFetchError("invalid JSON response") from None
    summary = json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )[:1000]
    return _evidence(
        content, _default_title(content.url), summary, "json", data
    )


def fetch_text(
    url: str,
    timeout: float = DEFAULT_SOURCE_TIMEOUT,
    max_chars: int = 4000,
    *,
    resolver: Optional[Resolver] = None,
    peer_getter: Optional[PeerGetter] = None,
) -> Dict[str, Any]:
    """取得有界文本，移除不可见标记并识别需浏览器交互的页面。"""
    positive_int("max_chars", max_chars)
    content = fetch_url(
        url, timeout=timeout, resolver=resolver, peer_getter=peer_getter
    )
    response_kind = classify_response(content)
    if response_kind == "binary":
        raise OnlineFetchError("binary response is not supported")
    value = decode_text(content.body, content.headers)
    if response_kind == "html":
        try:
            title, summary, requires_browser = analyze_html(value)
        except (AssertionError, ValueError):
            raise OnlineFetchError("invalid HTML response") from None
        if requires_browser:
            return {"fallback": "browser"}
    else:
        if has_browser_markers(value):
            return {"fallback": "browser"}
        title = _default_title(content.url)
        summary = " ".join(value.split())
    return _evidence(content, title, summary[:max_chars], "text", None)


def _source_label(source: Any, index: int) -> str:
    if isinstance(source, Mapping):
        label = source.get("source")
        if isinstance(label, str) and label.strip():
            safe_label = safe_source_label(label)
            if safe_label:
                return safe_label
    return "source-{}".format(index + 1)


def _fetch_source(
    source: Mapping[str, Any],
    remaining: float,
    fetchers: Fetchers,
) -> Dict[str, Any]:
    if not isinstance(source, Mapping):
        raise TypeError("source must be a mapping")
    kind = source.get("kind")
    url = source.get("url")
    if kind not in fetchers or not isinstance(url, str):
        raise ValueError("source requires a supported kind and URL")
    requested_timeout = positive_finite(
        "source timeout", source.get("timeout", DEFAULT_SOURCE_TIMEOUT)
    )
    timeout = min(requested_timeout, remaining)
    if kind == "text":
        max_chars = positive_int("max_chars", source.get("max_chars", 4000))
        result = fetchers[kind](url, timeout=timeout, max_chars=max_chars)
    else:
        result = fetchers[kind](url, timeout=timeout)
    if not isinstance(result, Mapping):
        raise TypeError("source adapter must return a mapping")
    return dict(result)


def _append_result(
    evidence: Dict[str, Any],
    label: str,
    append_evidence: Optional[Callable[[Dict[str, Any]], None]],
    errors: List[Dict[str, str]],
) -> int:
    if append_evidence is None:
        return 0
    try:
        append_evidence(evidence)
        return 1
    # 此处是来源隔离边界；任一存储适配器失败不能阻断后续来源。
    except Exception as error:
        errors.append(
            dict(source=label, stage="persist", error=type(error).__name__)
        )
        return 0


def _attempt_source(
    source: Mapping[str, Any],
    index: int,
    remaining: float,
    fetchers: Fetchers,
    errors: List[Dict[str, str]],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    label = _source_label(source, index)
    try:
        return label, _fetch_source(source, remaining, fetchers)
    # 来源编排是容错边界；任一 HTTP 适配器失败必须与后续来源隔离。
    except Exception as error:
        errors.append(
            dict(source=label, stage="fetch", error=type(error).__name__)
        )
        return label, None


def _result_status(
    total: int,
    evidence_count: int,
    error_count: int,
    fallback: Optional[str],
    deadline_reached: bool,
) -> str:
    if (
        evidence_count == total
        and error_count == 0
        and fallback is None
        and not deadline_reached
    ):
        return "complete"
    return "partial" if evidence_count else "failed"


def _gather_result(
    total: int,
    evidence: List[Dict[str, Any]],
    errors: List[Dict[str, str]],
    fallback: Optional[str],
    deadline_stage: Optional[str],
    attempted: int,
    persisted: int,
) -> Dict[str, Any]:
    deadline_reached = deadline_stage is not None
    return {
        "status": _result_status(
            total, len(evidence), len(errors), fallback, deadline_reached
        ),
        "evidence": evidence,
        "errors": errors,
        "fallback": fallback,
        "deadline_reached": deadline_reached,
        "deadline_stage": deadline_stage,
        "attempted": attempted,
        "persisted": persisted,
    }


def _default_fetchers(fetchers: Optional[Fetchers]) -> Fetchers:
    return fetchers if fetchers is not None else {
        "json": fetch_json, "text": fetch_text
    }


def gather_progressively(
    sources: Sequence[Mapping[str, Any]], deadline: float,
    append_evidence: Optional[Callable[[Dict[str, Any]], None]] = None,
    *, clock: Callable[[], float] = time.monotonic,
    fetchers: Optional[Fetchers] = None,
) -> Dict[str, Any]:
    """按顺序尝试来源，并在绝对单调时钟 deadline 前立即追加证据。"""
    deadline = finite_number("deadline", deadline)
    if not callable(clock):
        raise TypeError("clock must be callable")
    source_items = list(sources)
    active_fetchers = _default_fetchers(fetchers)
    evidence_items: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    fallback = deadline_stage = None
    attempted = persisted = 0
    for index, source in enumerate(source_items):
        remaining = _budget_remaining(deadline, clock)
        if remaining <= 0:
            deadline_stage = "source"
            break
        attempted += 1
        label, result = _attempt_source(
            source, index, remaining, active_fetchers, errors
        )
        if _budget_remaining(deadline, clock) <= 0:
            deadline_stage = "fetch"
            break
        if result is None:
            continue
        if result.get("fallback") == "browser":
            fallback = "browser"
            continue
        evidence = dict(result)
        evidence_items.append(evidence)
        if append_evidence is not None and _deadline_crossed(deadline, clock):
            deadline_stage = "persist"
            break
        persisted += _append_result(evidence, label, append_evidence, errors)
        if append_evidence is not None and _deadline_crossed(deadline, clock):
            deadline_stage = "persist"
            break
    return _gather_result(
        len(source_items), evidence_items, errors, fallback, deadline_stage,
        attempted, persisted,
    )
