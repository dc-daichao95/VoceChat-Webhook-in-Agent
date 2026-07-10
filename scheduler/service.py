"""Run reliable queue maintenance independently from Cursor inference."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlparse

from brain.pull import WebDAVClient, pull_conversations
from scheduler.db import QueueDB
from scheduler.ingest import ingest_downloaded_conversations
from scheduler.notifier import NotificationStats, process_due_notifications
from scheduler.policy import next_poll_seconds


class AlreadyRunningError(RuntimeError):
    """Indicate that another scheduler process owns the service lock."""


@dataclass(frozen=True)
class SchedulerConfig:
    """Validated scheduler paths, credentials, and timing policy."""

    db_path: Path
    state_path: Path
    inbound_dir: Path
    health_path: Path
    lock_path: Path
    vocechat_server: str
    vocechat_api_key: str
    webdav_url: str
    webdav_user: str
    webdav_password: str
    remote_dir: str = "conversations/"
    active_interval: int = 15
    normal_interval: int = 30
    idle_interval: int = 120
    quiet_interval: int = 300
    quiet_start: int = 0
    quiet_end: int = 7
    retry_base_seconds: int = 5
    retry_max_seconds: int = 300

    def __post_init__(self) -> None:
        """Normalize paths and reject unsafe or non-progressing settings."""
        for name in (
            "db_path", "state_path", "inbound_dir", "health_path", "lock_path"
        ):
            object.__setattr__(self, name, Path(getattr(self, name)))
        _validate_config(self)

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, str], root: Path
    ) -> "SchedulerConfig":
        """Build strict configuration without echoing sensitive values."""
        try:
            required = _required_config(values)
            root = Path(root)
            return cls(
                db_path=_path(values, "SCHEDULER_DB", root / "data/queue.db"),
                state_path=_path(values, "SCHEDULER_STATE", root / "data/state.json"),
                inbound_dir=_path(values, "SCHEDULER_INBOUND", root / "data/inbound"),
                health_path=_path(
                    values, "SCHEDULER_HEALTH",
                    root / "data/scheduler-health.json",
                ),
                lock_path=_path(values, "SCHEDULER_LOCK", root / "data/scheduler.lock"),
                remote_dir=values.get("WEBDAV_REMOTE_DIR", "conversations/"),
                active_interval=_integer(values, "SCHEDULER_ACTIVE_INTERVAL", 15),
                normal_interval=_integer(values, "SCHEDULER_NORMAL_INTERVAL", 30),
                idle_interval=_integer(values, "SCHEDULER_IDLE_MAX_INTERVAL", 120),
                quiet_interval=_integer(values, "SCHEDULER_QUIET_INTERVAL", 300),
                quiet_start=_hour(values, "SCHEDULER_QUIET_START", 0),
                quiet_end=_hour(values, "SCHEDULER_QUIET_END", 7),
                retry_base_seconds=_integer(
                    values, "SCHEDULER_ERROR_BACKOFF_INITIAL", 5
                ),
                retry_max_seconds=_integer(
                    values, "SCHEDULER_ERROR_BACKOFF_MAX", 300
                ),
                **required,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("invalid scheduler configuration") from error


@dataclass(frozen=True)
class TickResult:
    """Structured, serializable outcome of one scheduler round."""

    last_tick_at: int
    next_tick_at: int
    next_poll_seconds: int
    idle_rounds: int
    recovered: int
    new_jobs: int
    notifications: NotificationStats
    statuses: Dict[str, int]
    errors: tuple


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name)
    return value.strip()


def _required_config(values: Mapping[str, str]) -> Dict[str, str]:
    names = (
        ("vocechat_server", "VOCECHAT_SERVER_URL"),
        ("vocechat_api_key", "VOCECHAT_API_KEY"),
        ("webdav_url", "WEBDAV_URL"),
        ("webdav_user", "WEBDAV_USER"),
        ("webdav_password", "WEBDAV_PASSWORD"),
    )
    return {
        field: _required(values, environment)
        for field, environment in names
    }


def _integer(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise ValueError(name)
    return int(raw)


def _hour(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    if isinstance(raw, str):
        parts = raw.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1] == "00":
            return int(parts[0])
    return _integer(values, name, default)


def _path(values: Mapping[str, str], name: str, default: Path) -> Path:
    raw = values.get(name)
    return default if raw is None else Path(raw)


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _validate_config(config: SchedulerConfig) -> None:
    secret_fields = (
        config.vocechat_server, config.vocechat_api_key, config.webdav_url,
        config.webdav_user, config.webdav_password, config.remote_dir,
    )
    intervals = (
        config.active_interval, config.normal_interval, config.idle_interval,
        config.quiet_interval, config.retry_base_seconds,
        config.retry_max_seconds,
    )
    problems = (
        not all(isinstance(value, str) and value.strip() for value in secret_fields),
        not _valid_http_url(config.vocechat_server),
        not _valid_http_url(config.webdav_url),
        any(type(value) is not int or value <= 0 for value in intervals),
        config.idle_interval < config.normal_interval,
        config.retry_max_seconds < config.retry_base_seconds,
        not 0 <= config.quiet_start <= 23,
        not 0 <= config.quiet_end <= 23,
    )
    if any(problems):
        raise ValueError("invalid scheduler configuration")


def _load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default)
    return value if isinstance(value, dict) else dict(default)


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True, allow_nan=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_health(path: Path) -> Dict[str, Any]:
    """Read a persisted health snapshot, returning an empty object if absent."""
    return _load_json(Path(path), {})


class PidFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "PidFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="ascii")
        try:
            self._lock()
        except (OSError, BlockingIOError) as error:
            self.handle.close()
            self.handle = None
            raise AlreadyRunningError("scheduler already running") from error
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def _lock(self) -> None:
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
            try:
                self.path.unlink()
            except OSError:
                pass


class SchedulerService:
    """Coordinate recovery, WebDAV ingestion, SLA notices, and health."""

    def __init__(
        self,
        config: SchedulerConfig,
        *,
        db: Optional[Any] = None,
        clock: Optional[Any] = None,
        webdav_client: Optional[Any] = None,
        puller: Callable[..., dict] = pull_conversations,
        ingester: Callable[..., int] = ingest_downloaded_conversations,
        notifier: Callable[..., NotificationStats] = process_due_notifications,
        sender: Optional[Callable[..., Any]] = None,
        sleeper: Optional[Callable[[float], Any]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Inject all clocks and external effects for deterministic tests."""
        self.config = config
        self.db = db or QueueDB(config.db_path)
        self.clock = clock or datetime.now
        self.webdav_client = webdav_client or WebDAVClient(
            config.webdav_url,
            config.webdav_user,
            config.webdav_password,
            verify=False,
        )
        self.puller = puller
        self.ingester = ingester
        self.notifier = notifier
        self.sender = sender
        self._stop = threading.Event()
        self.sleep = sleeper or self._stop.wait
        self.logger = logger or logging.getLogger(__name__)
        self.idle_rounds = 0
        # Pull is the only external, failure-prone stage; it gets its own
        # circuit breaker so recover/ingest/notify keep meeting SLA deadlines.
        self._pull_backoff = config.retry_base_seconds
        self._pull_retry_at_ms = 0
        self._state = _load_json(
            config.state_path, {"conversations": {}, "seen_mids": []}
        )

    def stop(self) -> None:
        """Request a clean exit from the long-running loop."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        """Return whether shutdown has been requested."""
        return self._stop.is_set()

    def _now(self) -> datetime:
        value = self.clock() if callable(self.clock) else self.clock.now()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        return value

    def _log_error(self, phase: str, error: BaseException) -> None:
        event = {
            "event": "scheduler_phase_failed",
            "phase": phase,
            "error": type(error).__name__,
        }
        self.logger.error(json.dumps(event, separators=(",", ":")))

    def _run_phase(
        self, phase: str, operation: Callable[[], Any], default: Any,
        errors: list,
    ) -> Any:
        try:
            return operation()
        except Exception as error:
            errors.append(phase)
            self._log_error(phase, error)
            return default

    def _pull(self) -> None:
        updated = self.puller(
            self.webdav_client,
            self.config.remote_dir,
            self._state,
            self.config.inbound_dir,
        )
        if not isinstance(updated, dict):
            raise TypeError("pull state must be an object")
        self._state = updated
        _write_json(self.config.state_path, self._state)

    def _maybe_pull(self, now_ms: int, errors: list) -> None:
        # Skip while the breaker is open so a stuck WebDAV endpoint never
        # throttles the loop below its normal SLA polling cadence.
        if now_ms < self._pull_retry_at_ms:
            return
        before = len(errors)
        self._run_phase("pull", self._pull, None, errors)
        if len(errors) > before:
            self._pull_retry_at_ms = now_ms + self._pull_backoff * 1000
            self._pull_backoff = min(
                self._pull_backoff * 2, self.config.retry_max_seconds
            )
        else:
            self._pull_backoff = self.config.retry_base_seconds
            self._pull_retry_at_ms = 0

    def _notification_metrics(self, value: Any) -> NotificationStats:
        if isinstance(value, NotificationStats):
            return value
        if not isinstance(value, dict):
            raise TypeError("notifier stats must be structured")
        names = ("sent", "failed", "uncertain", "skipped", "storage_errors")
        return NotificationStats(**{n: int(value.get(n, 0)) for n in names})

    def _status_counts(self) -> Dict[str, int]:
        counts = {
            name: 0
            for name in ("pending", "processing", "retry_wait", "done", "cancelled")
        }
        # sqlite3 connections are not closed by their context manager, which only
        # scopes the transaction; a resident loop must close each handle itself.
        connection = sqlite3.connect(str(self.db.path), timeout=10)
        try:
            rows = connection.execute(
                "SELECT status,COUNT(*) FROM jobs GROUP BY status"
            ).fetchall()
        finally:
            connection.close()
        counts.update({status: count for status, count in rows})
        return counts

    def _interval(self, had_message: bool) -> int:
        # The service intentionally uses only 15s (active), 30s (normal) and
        # 120s (idle cap); it skips the policy's 60s intermediate tier because a
        # single coarse idle step keeps behaviour predictable for operators.
        policy_rounds = 0 if self.idle_rounds <= 10 else 4
        return next_poll_seconds(
            policy_rounds,
            had_message,
            self._now(),
            active=self.config.active_interval,
            normal=self.config.normal_interval,
            idle_max=self.config.idle_interval,
            quiet=self.config.quiet_interval,
            quiet_start=self.config.quiet_start,
            quiet_end=self.config.quiet_end,
        )

    def _finish_tick(
        self,
        now_ms: int,
        recovered: int,
        ingested: int,
        metrics: NotificationStats,
        errors: list,
    ) -> TickResult:
        self.idle_rounds = 0 if ingested else self.idle_rounds + 1
        interval = self._interval(bool(ingested))
        statuses = self._run_phase("status", self._status_counts, {}, errors)
        result = TickResult(
            last_tick_at=now_ms,
            next_tick_at=now_ms + interval * 1000,
            next_poll_seconds=interval,
            idle_rounds=self.idle_rounds,
            recovered=int(recovered),
            new_jobs=int(ingested),
            notifications=metrics,
            statuses=statuses,
            errors=tuple(errors),
        )
        self._run_phase(
            "health",
            lambda: _write_json(self.config.health_path, asdict(result)),
            None, errors,
        )
        return result

    def tick(self) -> TickResult:
        """Run one isolated, ordered maintenance round."""
        now_ms = int(self._now().timestamp() * 1000)
        errors = []
        recovered = self._run_phase(
            "recover", lambda: self.db.recover_expired(now_ms), 0, errors
        )
        self._maybe_pull(now_ms, errors)
        ingested = self._run_phase(
            "ingest",
            lambda: self.ingester(
                self.db, self.config.inbound_dir, self._state, now_ms
            ),
            0,
            errors,
        )
        empty_stats = NotificationStats()
        stats = self._run_phase(
            "notify",
            lambda: self.notifier(
                self.db,
                self.config.vocechat_server,
                self.config.vocechat_api_key,
                now_ms,
                sender=self.sender,
            ),
            empty_stats,
            errors,
        )
        metrics = self._run_phase(
            "notify_stats",
            lambda: self._notification_metrics(stats),
            empty_stats,
            errors,
        )
        return self._finish_tick(
            now_ms, recovered, ingested, metrics, errors
        )

    def read_health(self) -> Dict[str, Any]:
        """Return the last persisted non-sensitive health snapshot."""
        return read_health(self.config.health_path)

    def run_forever(self) -> None:
        """Run one locked service loop until stopped or interrupted."""
        # The loop always waits the SLA-driven poll interval; pull failures are
        # absorbed by the per-stage circuit breaker, never by the loop cadence.
        with PidFileLock(self.config.lock_path):
            try:
                while not self._stop.is_set():
                    result = self.tick()
                    self.sleep(result.next_poll_seconds)
            except KeyboardInterrupt:
                self.stop()
