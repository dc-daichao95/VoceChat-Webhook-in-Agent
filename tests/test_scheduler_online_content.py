"""HTTP 内容类型与基于可见 DOM 的浏览器回退测试。"""

import pytest
import responses

from scheduler.online import (
    FetchedContent,
    OnlineFetchError,
    classify_response,
    fetch_json,
    fetch_text,
)


PUBLIC_IP = "93.184.216.34"


def public_resolver(hostname, port):
    """为 responses 测试返回固定公网地址。"""
    return [PUBLIC_IP]


def public_peer(response):
    """为无 socket 的 responses 响应返回固定 peer。"""
    return PUBLIC_IP


def fetch_options():
    """返回明确的测试网络安全依赖。"""
    return {
        "resolver": public_resolver,
        "peer_getter": public_peer,
    }


@responses.activate
@pytest.mark.parametrize(
    "body",
    (
        "<html><body><form><input type='password'></form></body></html>",
        "<html><body><div id='captcha-challenge'>Continue</div></body></html>",
        (
            "<html><body><div id='app-root'></div>"
            "<script src='one.js'></script><script src='two.js'></script>"
            "<script src='three.js'></script></body></html>"
        ),
    ),
)
def test_fetch_text_uses_structural_dom_signals_for_browser_fallback(body):
    """密码表单、挑战元素及空 SPA 根节点应触发浏览器回退。"""
    url = "https://example.com/protected"
    responses.add(
        responses.GET, url, body=body, content_type="text/html", status=200
    )

    assert fetch_text(url, timeout=5, **fetch_options()) == {
        "fallback": "browser"
    }


@responses.activate
def test_fetch_text_does_not_misclassify_static_article_mentions():
    """静态文章提及 login/captcha 时不能仅凭关键词回退。"""
    url = "https://example.com/article"
    body = (
        "<html><title>Security guide</title><body><article>"
        "<h1>How login and CAPTCHA systems work</h1>"
        "<p>This educational article describes login forms and CAPTCHA "
        "history for developers. It contains ordinary readable prose, no "
        "interactive challenge, and enough useful content to answer a "
        "question directly without running browser-side JavaScript.</p>"
        "</article></body></html>"
    )
    responses.add(
        responses.GET, url, body=body, content_type="text/html", status=200
    )

    result = fetch_text(url, timeout=5, **fetch_options())

    assert result["kind"] == "text"
    assert result["title"] == "Security guide"
    assert "educational article" in result["summary"]


@responses.activate
@pytest.mark.parametrize("fetcher", (fetch_text, fetch_json))
def test_text_and_json_modes_reject_binary_without_decoding(fetcher):
    """文本与 JSON 入口不得把二进制 MIME 正文替换解码为文本。"""
    url = "https://example.com/archive.bin"
    responses.add(
        responses.GET,
        url,
        body=b"apparently-readable-but-binary",
        content_type="application/octet-stream",
        status=200,
    )

    with pytest.raises(OnlineFetchError) as error:
        fetcher(url, timeout=5, **fetch_options())

    assert str(error.value) == "binary response is not supported"


def test_classify_response_uses_content_type_and_safe_sniffing():
    """响应类型应优先看 Content-Type，并能处理缺失类型。"""
    assert classify_response(
        FetchedContent(
            "https://example.com/a",
            200,
            {"Content-Type": "application/problem+json"},
            b"{}",
        )
    ) == "json"
    assert classify_response(
        FetchedContent(
            "https://example.com/a",
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            b"<html></html>",
        )
    ) == "html"
    assert classify_response(
        FetchedContent("https://example.com/a", 200, {}, b"plain")
    ) == "text"


@responses.activate
def test_fetch_json_returns_complete_structured_evidence():
    """JSON 快路径应返回统一证据字段和解码后的数据。"""
    url = "https://example.com/weather.json"
    responses.add(
        responses.GET,
        url,
        json={"temperature": 30},
        status=200,
    )

    result = fetch_json(url, timeout=5, **fetch_options())

    assert set(result) == {
        "source",
        "url",
        "title",
        "summary",
        "kind",
        "data",
    }
    assert result["kind"] == "json"
    assert result["source"] == "example.com"
    assert result["data"]["temperature"] == 30


@responses.activate
@pytest.mark.parametrize(
    "body,content_type",
    (
        ("<html><body>ordinary page</body></html>", "text/html"),
        ("<body>ordinary HTML fragment</body>", "application/json"),
        (
            "<html><body>Please sign in to continue</body></html>",
            "application/json",
        ),
        ("Please enable JavaScript to continue", "text/plain"),
        ("Verify you are human: CAPTCHA", "text/plain"),
    ),
)
def test_fetch_json_routes_html_and_interactive_shells_to_browser(
    body, content_type
):
    """JSON 入口实际取得 HTML 或交互壳时应回退浏览器。"""
    url = "https://example.com/api"
    responses.add(
        responses.GET,
        url,
        body=body,
        content_type=content_type,
        status=200,
    )

    assert fetch_json(url, timeout=5, **fetch_options()) == {
        "fallback": "browser"
    }


@responses.activate
def test_fetch_json_sanitizes_ordinary_invalid_json_error():
    """普通无效 JSON 仍应失败，但异常不得携带 URL 凭据或正文。"""
    url = "https://example.com/api?session=top-secret"
    responses.add(
        responses.GET,
        url,
        body="private-invalid-body",
        content_type="application/json",
        status=200,
    )

    with pytest.raises(OnlineFetchError) as error:
        fetch_json(url, timeout=5, **fetch_options())

    assert str(error.value) == "invalid JSON response"
    assert "top-secret" not in repr(error.value)
    assert "private-invalid-body" not in repr(error.value)
    assert error.value.__cause__ is None


@responses.activate
def test_fetch_json_redacts_sensitive_query_values_from_evidence():
    """成功证据不得持久化任何查询参数名或参数值。"""
    url = "https://example.com/weather?api_key=top-secret&city=shanghai"
    responses.add(
        responses.GET,
        url,
        json={"temperature": 30},
        status=200,
    )

    result = fetch_json(url, timeout=5, **fetch_options())

    assert "top-secret" not in result["url"]
    assert result["url"] == "https://example.com/weather"


@responses.activate
def test_fetch_text_strips_markup_scripts_styles_and_limits_summary():
    """文本快路径应提取标题、正文并排除不可见脚本和样式。"""
    url = "https://example.com/page"
    body = (
        "<html><title>T</title><style>private-style</style>"
        "<body>Hello <b>world</b><script>private-script</script></body></html>"
    )
    responses.add(responses.GET, url, body=body, status=200)

    result = fetch_text(
        url, timeout=5, max_chars=11, **fetch_options()
    )

    assert result["title"] == "T"
    assert result["summary"] == "Hello world"
    assert result["kind"] == "text"
    assert result["data"] is None
    assert "private" not in result["summary"]


@responses.activate
@pytest.mark.parametrize(
    "body",
    (
        "<html><body>Please enable JavaScript to continue</body></html>",
        "<html><body>Verify you are human: CAPTCHA</body></html>",
        "<html><body>Please sign in to view this page</body></html>",
    ),
)
def test_fetch_text_routes_js_captcha_and_login_pages_to_browser(body):
    """交互壳、验证码和登录墙应快速返回 browser 回退。"""
    url = "https://example.com/protected"
    responses.add(responses.GET, url, body=body, status=200)

    assert fetch_text(url, timeout=5, **fetch_options()) == {
        "fallback": "browser"
    }
