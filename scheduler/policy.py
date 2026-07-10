"""Pure timing policies for the reliable scheduler."""

from __future__ import annotations

from datetime import datetime
from numbers import Real
from typing import Literal, Optional


def _require_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _require_positive_int(name: str, value: object) -> int:
    value = _require_int(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _require_hour(name: str, value: object) -> int:
    value = _require_int(name, value)
    if not 0 <= value <= 23:
        raise ValueError(f"{name} must be between 0 and 23")
    return value


def is_quiet_hour(
    now: datetime, quiet_start: int = 0, quiet_end: int = 7
) -> bool:
    """Return whether ``now`` falls in the configured local-hour window."""
    quiet_start = _require_hour("quiet_start", quiet_start)
    quiet_end = _require_hour("quiet_end", quiet_end)
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= now.hour < quiet_end
    return now.hour >= quiet_start or now.hour < quiet_end


def next_poll_seconds(
    idle_rounds: int,
    had_message: bool,
    now: datetime,
    active: int = 15,
    normal: int = 30,
    idle_max: int = 120,
    quiet: int = 300,
    quiet_start: int = 0,
    quiet_end: int = 7,
) -> int:
    """Choose a validated poll delay from quiet, active, and idle state."""
    active = _require_positive_int("active", active)
    normal = _require_positive_int("normal", normal)
    idle_max = _require_positive_int("idle_max", idle_max)
    quiet = _require_positive_int("quiet", quiet)
    idle_rounds = _require_int("idle_rounds", idle_rounds)
    if idle_max < normal:
        raise ValueError("idle_max must be greater than or equal to normal")
    if is_quiet_hour(now, quiet_start, quiet_end):
        return quiet
    if had_message:
        return active
    idle_rounds = max(idle_rounds, 0)
    if idle_rounds <= 2:
        return normal
    if idle_rounds == 3:
        return min(normal * 2, idle_max)
    return idle_max


def retry_delay_seconds(attempts: int, cap_seconds: int = 1800) -> int:
    """Use persisted one-based attempts directly, without incrementing them."""
    attempts = _require_int("attempts", attempts)
    cap_seconds = _require_positive_int("cap_seconds", cap_seconds)
    schedule = (60, 300, 900, 1800)
    index = min(max(attempts, 1), len(schedule)) - 1
    return min(schedule[index], cap_seconds, 1800)


def sla_action(
    age_seconds: float,
    ack_sent: bool,
    partial_sent: bool,
    has_evidence: bool,
) -> Optional[Literal["ack", "partial", "status"]]:
    """Return an SLA action for unfinished tasks.

    ``partial_sent`` means a partial or status update has already been sent.
    """
    if isinstance(age_seconds, bool) or not isinstance(age_seconds, Real):
        raise TypeError("age_seconds must be numeric")
    age_seconds = max(age_seconds, 0)
    if partial_sent:
        return None
    if age_seconds >= 45:
        return "partial" if has_evidence else "status"
    if age_seconds >= 10 and not ack_sent:
        return "ack"
    return None
