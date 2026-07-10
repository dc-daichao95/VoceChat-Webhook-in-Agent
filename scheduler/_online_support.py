"""HTTP 快路径内部使用的严格输入验证与内容安全辅助函数。"""

from __future__ import annotations

import json
import math
import re
from html.parser import HTMLParser
from typing import List, Mapping, Optional, Tuple
from urllib.parse import (
    unquote,
    urlsplit,
    urlunsplit,
)

_BROWSER_MARKERS = (
    "enable javascript",
    "javascript is required",
    "login required",
    "please log in",
    "please sign in",
    "sign in to continue",
    "verify you are human",
)
_PATH_KEYS = frozenset(
    (
        "access-token",
        "access_token",
        "api-key",
        "api_key",
        "apikey",
        "auth",
        "bearer",
        "credential",
        "cookie",
        "key",
        "passwd",
        "password",
        "secret",
        "session",
        "signature",
        "token",
    )
)
_ALWAYS_REDACT_PATH_KEYS = _PATH_KEYS
MAX_SOURCE_LABEL_CHARS = 64
MAX_SOURCE_URL_CHARS = 200
_SAFE_SOURCE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH_ASSIGNMENT = re.compile(
    r"^(?P<key>[A-Za-z0-9_-]+)(?P<separator>[=:])(?P<value>.+)$"
)
_JWT = re.compile(
    r"^eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"
)


class _HTMLTextExtractor(HTMLParser):
    """提取可见 DOM，并记录交互墙与空 SPA 的结构信号。"""

    _IGNORED = frozenset(("script", "style", "noscript", "template"))
    _BLOCKS = frozenset(
        (
            "article", "br", "div", "footer", "h1", "h2", "h3", "h4",
            "h5", "h6", "header", "li", "main", "p", "section", "tr",
        )
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self._form_depth = 0
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []
        self.password_form = False
        self.challenge_element = False
        self.app_root = False
        self.script_count = 0

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        attributes = {
            key.lower(): (value or "").lower() for key, value in attrs
        }
        if tag == "form":
            self._form_depth += 1
        if (
            tag == "input"
            and self._form_depth
            and attributes.get("type") == "password"
        ):
            self.password_form = True
        self.challenge_element |= _has_challenge_attribute(attributes)
        self.app_root |= _has_app_root_attribute(attributes)
        if tag == "script":
            self.script_count += 1
        if tag in self._IGNORED:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in self._BLOCKS:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._form_depth:
            self._form_depth -= 1
        if tag in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag in self._BLOCKS:
            self.text_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


def _has_challenge_attribute(attributes: Mapping[str, str]) -> bool:
    """识别验证码或挑战控件的语义属性。"""
    names = ("id", "class", "name", "aria-label", "data-sitekey", "src")
    joined = " ".join(attributes.get(name, "") for name in names)
    return "captcha" in joined or "challenge" in joined


def _has_app_root_attribute(attributes: Mapping[str, str]) -> bool:
    """识别常见前端应用空根节点。"""
    values = "{} {}".format(
        attributes.get("id", ""), attributes.get("class", "")
    )
    tokens = frozenset(re.findall(r"[a-z0-9_]+", values))
    return bool(tokens.intersection(("app", "root", "reactroot", "__next")))


def finite_number(name: str, value) -> float:
    """返回有限浮点数；拒绝隐式转换、布尔值及 nan/inf。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("{} must be an int or float".format(name))
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("{} must be finite".format(name))
    return converted


def positive_finite(name: str, value) -> float:
    """返回有限正数。"""
    converted = finite_number(name, value)
    if converted <= 0:
        raise ValueError("{} must be positive".format(name))
    return converted


def positive_int(
    name: str, value, maximum: Optional[int] = None
) -> int:
    """返回正整数，并可施加公共硬上限。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("{} must be an int".format(name))
    if value <= 0:
        raise ValueError("{} must be positive".format(name))
    if maximum is not None and value > maximum:
        raise ValueError("{} exceeds maximum".format(name))
    return value


def nonnegative_int(name: str, value) -> int:
    """返回非负整数，拒绝布尔值和隐式转换。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("{} must be an int".format(name))
    if value < 0:
        raise ValueError("{} must not be negative".format(name))
    return value


def _reject_json_constant(value: str):
    raise ValueError("non-finite JSON constant")


def _json_numbers_are_finite(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_numbers_are_finite(item) for item in value)
    if isinstance(value, dict):
        return all(_json_numbers_are_finite(item) for item in value.values())
    return True


def strict_json_loads(encoded: str):
    """解析标准 JSON，并递归拒绝所有非有限浮点数。"""
    value = json.loads(encoded, parse_constant=_reject_json_constant)
    if not _json_numbers_are_finite(value):
        raise ValueError("non-finite JSON number")
    return value


def normalize_url(url: str) -> str:
    """验证请求 URL，仅保留无 userinfo/fragment 的 HTTP(S) 形式。"""
    if not isinstance(url, str) or not url:
        raise ValueError("URL must be a non-empty string")
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("URL contains whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("URL scheme must be http or https")
    if (
        parts.hostname is None
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("URL must have a host and no user information")
    _ = parts.port
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc, parts.path, parts.query, "")
    )


def _looks_like_credential(value: str) -> bool:
    decoded = unquote(value)
    if _JWT.fullmatch(decoded):
        return True
    if len(decoded) < 12:
        return False
    return any(character.isdigit() or character in "-_." for character in decoded)


def _safe_path(path: str) -> str:
    segments = path.split("/")
    for index, segment in enumerate(segments):
        decoded = unquote(segment)
        if _JWT.fullmatch(decoded):
            segments[index] = "REDACTED"
            continue
        assignment = _PATH_ASSIGNMENT.fullmatch(decoded)
        if assignment and assignment.group("key").lower() in _PATH_KEYS:
            segments[index] = "{}{}REDACTED".format(
                assignment.group("key"), assignment.group("separator")
            )
            continue
        if decoded.lower() not in _PATH_KEYS or index + 1 >= len(segments):
            continue
        if (
            decoded.lower() in _ALWAYS_REDACT_PATH_KEYS
            or _looks_like_credential(segments[index + 1])
        ):
            segments[index + 1] = "REDACTED"
    return "/".join(segments)


def public_url(url: str) -> str:
    """生成可持久化 URL，删除查询与身份信息并净化路径凭据。"""
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(
        (
            parts.scheme,
            netloc,
            _safe_path(parts.path),
            "",
            "",
        )
    )


def safe_source_label(label: str) -> str:
    """只接受普通短来源名；URL 标签则执行完整凭据净化。"""
    cleaned = label.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        return ""
    parts = urlsplit(cleaned)
    if parts.scheme.lower() in ("http", "https"):
        return public_url(cleaned)[:MAX_SOURCE_URL_CHARS]
    if not _SAFE_SOURCE_LABEL.fullmatch(cleaned):
        return ""
    return cleaned[:MAX_SOURCE_LABEL_CHARS]


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return None


def decode_text(body: bytes, headers: Mapping[str, str]) -> str:
    """按响应 charset 解码正文，未知编码安全回退 UTF-8。"""
    content_type = _header(headers, "Content-Type") or ""
    match = re.search(
        r"charset\s*=\s*[\"']?([^;\"'\s]+)", content_type, re.I
    )
    charset = match.group(1) if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _parse_html(value: str) -> _HTMLTextExtractor:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser


def _normalized_html_text(
    parser: _HTMLTextExtractor,
) -> Tuple[str, str]:
    title = " ".join(" ".join(parser.title_parts).split())
    summary = " ".join(" ".join(parser.text_parts).split())
    return title, summary


def analyze_html(value: str) -> Tuple[str, str, bool]:
    """返回标题、可见正文及基于 DOM 结构判定的浏览器回退信号。"""
    parser = _parse_html(value)
    title, summary = _normalized_html_text(parser)
    short_action_wall = (
        len(summary) <= 160 and has_browser_markers(summary)
    )
    sparse_spa = len(summary) <= 80 and (
        parser.script_count >= 3
        or (parser.app_root and parser.script_count >= 1)
    )
    requires_browser = (
        parser.password_form
        or parser.challenge_element
        or short_action_wall
        or sparse_spa
    )
    return title, summary, requires_browser


def extract_html(value: str) -> Tuple[str, str]:
    """返回去除脚本和样式后的 HTML 标题与可见正文。"""
    title, summary, _ = analyze_html(value)
    return title, summary


def has_browser_markers(value: str) -> bool:
    """仅对简短、明确的操作墙文案判断浏览器交互。"""
    normalized = " ".join(value.split())
    if len(normalized) > 160:
        return False
    lowered = normalized.lower()
    return any(marker in lowered for marker in _BROWSER_MARKERS)
