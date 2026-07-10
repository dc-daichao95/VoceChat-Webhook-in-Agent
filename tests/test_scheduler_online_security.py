"""HTTP 快路径证据 URL 与错误信息净化测试。"""

import json

import pytest
import responses

import scheduler.online as online_module
from scheduler.online import (
    OnlineFetchError,
    UnsafeURLError,
    fetch_json,
    fetch_text,
    fetch_url,
    gather_progressively,
)

PUBLIC_IP = "93.184.216.34"


def public_resolver(hostname, port):
    """为 HTTP 测试返回固定公网地址。"""
    return [PUBLIC_IP]


def public_peer(response):
    """为 responses 替身注入已批准公网 peer。"""
    return PUBLIC_IP


@responses.activate
@pytest.mark.parametrize(
    "name",
    (
        "credential",
        "client_credential",
        "session",
        "session_id",
        "cookie",
        "set_cookie",
    ),
)
def test_evidence_redacts_additional_sensitive_query_names(name):
    """证据 URL 应删除全部查询字段和值，避免未知敏感参数漏出。"""
    secret = "sensitive-value-123"
    url = "https://example.com/api?{}={}&city=shanghai".format(
        name, secret
    )
    responses.add(responses.GET, url, json={"ok": True}, status=200)

    evidence = fetch_json(
        url,
        timeout=5,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    assert secret not in evidence["url"]
    assert evidence["url"] == "https://example.com/api"


def test_userinfo_is_rejected_without_echoing_credentials():
    """含 userinfo 的 URL 应拒绝，异常不得回显用户名或密码。"""
    url = "https://private-user:private-password@example.com/data"

    with pytest.raises(UnsafeURLError) as error:
        fetch_url(url)

    rendered = repr(error.value)
    assert "private-user" not in rendered
    assert "private-password" not in rendered


@responses.activate
def test_fragment_is_removed_from_request_and_evidence_url():
    """fragment 不应发往服务端或写入证据。"""
    requested = "https://example.com/data?city=shanghai"
    responses.add(
        responses.GET,
        requested,
        json={"ok": True},
        status=200,
    )

    evidence = fetch_json(
        requested + "#private-fragment-token",
        timeout=5,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    assert evidence["url"] == "https://example.com/data"
    assert "private-fragment-token" not in evidence["url"]


@responses.activate
@pytest.mark.parametrize(
    "path,secret",
    (
        ("/v1/token/sensitive-value-123/report", "sensitive-value-123"),
        ("/download/session=sensitive-value-123/file", "sensitive-value-123"),
        (
            "/asset/eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxIn0.private-signature/report",
            "private-signature",
        ),
    ),
)
def test_evidence_conservatively_redacts_obvious_path_credentials(
    path, secret
):
    """明显 token/session/JWT 路径值不得写入证据 URL。"""
    url = "https://example.com{}".format(path)
    responses.add(responses.GET, url, json={"ok": True}, status=200)

    evidence = fetch_json(
        url,
        timeout=5,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    assert secret not in evidence["url"]
    assert "REDACTED" in evidence["url"]


@responses.activate
@pytest.mark.parametrize(
    "key",
    (
        "token",
        "key",
        "secret",
        "credential",
        "password",
        "session",
        "passwd",
        "api-key",
        "apikey",
        "access-token",
        "bearer",
        "auth",
    ),
)
def test_evidence_always_redacts_value_after_credential_path_key(key):
    """明确凭据路径键后的值不论形态或长度都必须脱敏。"""
    url = "https://example.com/v1/{}/x/report".format(key)
    responses.add(responses.GET, url, json={"ok": True}, status=200)

    evidence = fetch_json(
        url,
        timeout=5,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    assert evidence["url"] == (
        "https://example.com/v1/{}/REDACTED/report".format(key)
    )


@responses.activate
def test_evidence_preserves_ordinary_security_related_paths():
    """普通文档路径即使含安全相关单词也不得被无端改写。"""
    path = "/guides/session-management/cookie-recipes/authentication"
    url = "https://example.com{}".format(path)
    responses.add(responses.GET, url, json={"ok": True}, status=200)

    evidence = fetch_json(
        url,
        timeout=5,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    assert evidence["url"] == url


def test_source_url_in_structured_error_is_sanitized():
    """来源标签若为 URL，错误结果也不得泄漏其各类凭据。"""
    source_label = (
        "https://private-user:private-password@example.com/"
        "token/sensitive-value-123?credential=query-secret"
        "#fragment-secret"
    )

    def broken(*args, **kwargs):
        raise RuntimeError("failure")

    result = gather_progressively(
        [
            {
                "source": source_label,
                "kind": "json",
                "url": "https://example.com/api",
            }
        ],
        deadline=10,
        clock=lambda: 0.0,
        fetchers={"json": broken},
    )

    rendered = result["errors"][0]["source"]
    for secret in (
        "private-user",
        "private-password",
        "sensitive-value-123",
        "query-secret",
        "fragment-secret",
    ):
        assert secret not in rendered
    assert "?" not in rendered


@pytest.mark.parametrize(
    "source_label",
    (
        "api_key=top-secret",
        "weather\x00\r\nsecret",
        "x" * 65,
        "../private-source",
        "private source",
        "来源-secret",
    ),
)
def test_invalid_non_url_source_label_falls_back_without_leaking(source_label):
    """非普通短来源名不得经清洗后进入错误，必须回退稳定编号。"""

    result = gather_progressively(
        [
            {
                "source": source_label,
                "kind": "json",
                "url": "https://example.com/api",
            }
        ],
        deadline=10,
        clock=lambda: 0.0,
        fetchers={"json": lambda *args, **kwargs: None},
    )

    rendered = result["errors"][0]["source"]
    assert rendered == "source-1"
    assert "top-secret" not in json.dumps(result)


def test_safe_short_non_url_source_label_is_preserved():
    """普通 ASCII 来源名可保留，便于错误定位。"""
    result = gather_progressively(
        [
            {
                "source": "weather-api_1.v2",
                "kind": "json",
                "url": "https://example.com/api",
            }
        ],
        deadline=10,
        clock=lambda: 0.0,
        fetchers={"json": lambda *args, **kwargs: None},
    )

    assert result["errors"][0]["source"] == "weather-api_1.v2"


@responses.activate
def test_html_parser_error_does_not_expose_original_details(monkeypatch):
    """HTML 解析异常链不得把原始正文细节带入上层日志。"""
    url = "https://example.com/page"
    responses.add(
        responses.GET,
        url,
        body="<html><body>private-body</body></html>",
        content_type="text/html",
        status=200,
    )

    def broken_parser(value):
        raise ValueError("private-body")

    monkeypatch.setattr(online_module, "analyze_html", broken_parser)

    with pytest.raises(OnlineFetchError) as error:
        fetch_text(
            url,
            timeout=5,
            resolver=public_resolver,
            peer_getter=public_peer,
        )

    assert str(error.value) == "invalid HTML response"
    assert "private-body" not in repr(error.value)
    assert error.value.__cause__ is None
