"""Shared safe persisted error categories."""

from __future__ import annotations

import re


SAFE_ERROR_CATEGORIES = frozenset(
    (
        "InternalError",
        "InvalidResponse",
        "LeaseExpired",
        "ProcessCrashed",
        "SettlementError",
        "Timeout",
        "TransportError",
    )
)
_HTTP_STATUS = re.compile(r"HTTP [1-5][0-9]{2}")


def safe_error_category(error: str) -> str:
    """Return only a fixed category or a syntactically valid HTTP status."""
    if error in SAFE_ERROR_CATEGORIES:
        return error
    if isinstance(error, str) and _HTTP_STATUS.fullmatch(error):
        return error
    return "InternalError"
