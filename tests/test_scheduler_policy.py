"""Tests for scheduler timing policies."""

from datetime import datetime

import pytest

from scheduler.policy import (
    is_quiet_hour,
    next_poll_seconds,
    retry_delay_seconds,
    sla_action,
)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 7, 9, 23, 59), False),
        (datetime(2026, 7, 10, 0, 0), True),
        (datetime(2026, 7, 10, 6, 59), True),
        (datetime(2026, 7, 10, 7, 0), False),
    ],
)
def test_is_quiet_hour_uses_default_boundaries(now, expected):
    assert is_quiet_hour(now) is expected


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(21, False), (22, True), (0, True), (6, True), (7, False)],
)
def test_is_quiet_hour_supports_windows_crossing_midnight(hour, expected):
    now = datetime(2026, 7, 10, hour, 0)
    assert is_quiet_hour(now, quiet_start=22, quiet_end=7) is expected


def test_equal_quiet_hour_boundaries_disable_quiet_mode():
    now = datetime(2026, 7, 10, 3, 0)
    assert is_quiet_hour(now, quiet_start=3, quiet_end=3) is False


@pytest.mark.parametrize("field", ["quiet_start", "quiet_end"])
@pytest.mark.parametrize("value", [True, 1.5, "7", None])
def test_quiet_hour_rejects_non_integer_boundaries(field, value):
    options = {"quiet_start": 0, "quiet_end": 7, field: value}
    with pytest.raises(TypeError, match=field):
        is_quiet_hour(datetime(2026, 7, 10, 3, 0), **options)


@pytest.mark.parametrize("field", ["quiet_start", "quiet_end"])
@pytest.mark.parametrize("value", [-1, 24])
def test_quiet_hour_rejects_boundaries_outside_day(field, value):
    options = {"quiet_start": 0, "quiet_end": 7, field: value}
    with pytest.raises(ValueError, match=field):
        is_quiet_hour(datetime(2026, 7, 10, 3, 0), **options)


def test_quiet_hours_always_poll_every_five_minutes():
    now = datetime(2026, 7, 10, 6, 59)
    assert next_poll_seconds(idle_rounds=0, had_message=True, now=now) == 300
    assert next_poll_seconds(idle_rounds=20, had_message=False, now=now) == 300


def test_message_uses_active_interval_outside_quiet_hours():
    now = datetime(2026, 7, 10, 7, 0)
    assert next_poll_seconds(idle_rounds=20, had_message=True, now=now) == 15


def test_polling_uses_custom_intervals_and_quiet_window():
    daytime = datetime(2026, 7, 10, 12, 0)
    nighttime = datetime(2026, 7, 10, 23, 0)
    options = {
        "active": 5,
        "normal": 20,
        "idle_max": 80,
        "quiet": 240,
        "quiet_start": 22,
        "quiet_end": 7,
    }

    assert next_poll_seconds(0, True, daytime, **options) == 5
    assert next_poll_seconds(0, False, daytime, **options) == 20
    assert next_poll_seconds(3, False, daytime, **options) == 40
    assert next_poll_seconds(4, False, daytime, **options) == 80
    assert next_poll_seconds(99, True, nighttime, **options) == 240


@pytest.mark.parametrize("field", ["active", "normal", "idle_max", "quiet"])
@pytest.mark.parametrize("value", [True, 1.5, "30", None])
def test_polling_rejects_non_integer_intervals(field, value):
    options = {"active": 15, "normal": 30, "idle_max": 120, "quiet": 300}
    options[field] = value
    with pytest.raises(TypeError, match=field):
        next_poll_seconds(
            0, False, datetime(2026, 7, 10, 12, 0), **options
        )


@pytest.mark.parametrize("field", ["active", "normal", "idle_max", "quiet"])
@pytest.mark.parametrize("value", [0, -1])
def test_polling_rejects_non_positive_intervals(field, value):
    options = {"active": 15, "normal": 30, "idle_max": 120, "quiet": 300}
    options[field] = value
    with pytest.raises(ValueError, match=field):
        next_poll_seconds(
            0, False, datetime(2026, 7, 10, 12, 0), **options
        )


def test_polling_rejects_idle_cap_below_normal_interval():
    with pytest.raises(ValueError, match="idle_max"):
        next_poll_seconds(
            0,
            False,
            datetime(2026, 7, 10, 12, 0),
            normal=60,
            idle_max=30,
        )


@pytest.mark.parametrize("idle_rounds", [True, 1.5, "3", None])
def test_polling_requires_integer_idle_rounds(idle_rounds):
    with pytest.raises(TypeError, match="idle_rounds"):
        next_poll_seconds(
            idle_rounds, False, datetime(2026, 7, 10, 12, 0)
        )


@pytest.mark.parametrize(
    ("idle_rounds", "expected"),
    [
        (-1, 30),
        (0, 30),
        (1, 30),
        (2, 30),
        (3, 60),
        (4, 120),
        (20, 120),
    ],
)
def test_idle_polling_backs_off_to_two_minute_cap(idle_rounds, expected):
    now = datetime(2026, 7, 10, 9, 0)
    assert next_poll_seconds(idle_rounds, had_message=False, now=now) == expected


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (-1, 60),
        (0, 60),
        (1, 60),
        (2, 300),
        (3, 900),
        (4, 1800),
        (99, 1800),
    ],
)
def test_retry_delay_follows_schedule_and_stays_capped(attempts, expected):
    assert retry_delay_seconds(attempts) == expected


@pytest.mark.parametrize(
    ("attempts", "cap_seconds", "expected"),
    [(1, 30, 30), (2, 120, 120), (3, 600, 600), (999_999, 1000, 1000)],
)
def test_retry_delay_respects_custom_cap(attempts, cap_seconds, expected):
    assert retry_delay_seconds(attempts, cap_seconds=cap_seconds) == expected


def test_retry_delay_never_exceeds_hard_thirty_minute_cap():
    assert retry_delay_seconds(99, cap_seconds=3600) == 1800


@pytest.mark.parametrize("attempts", [True, 1.5, "2", None])
def test_retry_delay_requires_integer_persisted_attempts(attempts):
    with pytest.raises(TypeError, match="attempts"):
        retry_delay_seconds(attempts)


@pytest.mark.parametrize("cap_seconds", [True, 1.5, "1800", None])
def test_retry_delay_requires_integer_cap(cap_seconds):
    with pytest.raises(TypeError, match="cap_seconds"):
        retry_delay_seconds(1, cap_seconds=cap_seconds)


@pytest.mark.parametrize("cap_seconds", [0, -1])
def test_retry_delay_rejects_non_positive_cap(cap_seconds):
    with pytest.raises(ValueError, match="cap_seconds"):
        retry_delay_seconds(1, cap_seconds=cap_seconds)


def test_sla_waits_until_ack_deadline():
    assert sla_action(9.999, False, False, False) is None
    assert sla_action(10, False, False, False) == "ack"


def test_sla_does_not_repeat_ack():
    assert sla_action(10, True, False, False) is None
    assert sla_action(44.999, True, False, False) is None


def test_sla_before_partial_deadline_still_requests_missing_ack():
    assert sla_action(44.999, False, False, False) == "ack"


@pytest.mark.parametrize(
    ("has_evidence", "expected"),
    [(True, "partial"), (False, "status")],
)
def test_sla_partial_deadline_takes_priority_over_missing_ack(
    has_evidence, expected
):
    assert sla_action(45, False, False, has_evidence) == expected


def test_sla_does_not_repeat_partial_or_status():
    assert sla_action(45, True, True, True) is None
    assert sla_action(10, False, True, False) is None
    assert sla_action(44.999, False, True, True) is None
    assert sla_action(60, False, True, False) is None


def test_sla_treats_negative_age_as_zero():
    assert sla_action(-1, False, False, True) is None


@pytest.mark.parametrize("age_seconds", [True, "10", None, object()])
def test_sla_requires_numeric_age(age_seconds):
    with pytest.raises(TypeError, match="age_seconds"):
        sla_action(age_seconds, False, False, False)
