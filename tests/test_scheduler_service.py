"""独立 scheduler service loop 的可靠性与命令行契约测试。"""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest
import responses

from brain.pull import WebDAVClient
from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.notifier import NotificationStats
from scheduler.outbox import Outbox
from scheduler.service import (
    AlreadyRunningError,
    PidFileLock,
    SchedulerConfig,
    SchedulerService,
)
from scripts import scheduler as scheduler_cli


class FakeClock:
    """提供可推进且不依赖真实等待的本地时钟。"""

    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class Response:
    """提供通知发送所需的最小成功响应。"""

    ok = True
    status_code = 200


def config(tmp_path, **overrides):
    """构造不包含真实凭据的完整服务配置。"""

    values = {
        "db_path": tmp_path / "queue.db",
        "state_path": tmp_path / "state.json",
        "inbound_dir": tmp_path / "inbound",
        "health_path": tmp_path / "health.json",
        "lock_path": tmp_path / "scheduler.lock",
        "webdav_url": "https://dav.example/share/",
        "webdav_user": "user",
        "webdav_password": "password",
        "remote_dir": "conversations/",
        "vocechat_server": "https://chat.example",
        "vocechat_api_key": "api-key",
    }
    values.update(overrides)
    return SchedulerConfig(**values)


def make_service(tmp_path, clock, **kwargs):
    """构造不访问真实网络的服务。"""

    defaults = {
        "puller": lambda client, remote, state, inbound: state,
        "ingester": lambda db, inbound, state, now: 0,
        "sender": lambda *args, **kwargs: Response(),
    }
    defaults.update(kwargs)
    return SchedulerService(config(tmp_path), clock=clock, **defaults)


def test_tick_recovers_expired_final_as_uncertain_before_other_stages(tmp_path):
    """过期 processing 与已预约 final 必须先冻结后回到 retry。"""

    clock = FakeClock(datetime(2026, 7, 10, 9, 0, 2))
    service = make_service(tmp_path, clock)
    job_id = service.db.enqueue({"conv_id": "u1", "mid": 1}, 1_000)
    assert service.db.claim("cursor", 1_000, 1, 1)
    record = {
        "conv_id": "u1",
        "mid": 1,
        "reply": "private",
        "markdown": False,
        "bot_uid": 7,
        "created_at": 1_100,
    }
    assert ConsumerQueue(service.db).prepare_final(
        job_id, "cursor", record, 1_100, 1
    )

    result = service.tick()

    assert result.recovered == 1
    assert service.db.get(job_id)["status"] == "retry_wait"
    assert Outbox(service.db).state(job_id, "final")["state"] == "uncertain"


def test_tick_stage_order_is_recover_pull_ingest_notify_health(tmp_path):
    """每轮阶段顺序固定，便于故障隔离和回放。"""

    events = []
    clock = FakeClock(datetime(2026, 7, 10, 9))

    class DB:
        path = str(tmp_path / "queue.db")

        def recover_expired(self, now):
            events.append("recover")
            return 0

    def puller(client, remote, state, inbound):
        events.append("pull")
        return state

    def ingester(db, inbound, state, now):
        events.append("ingest")
        return 0

    def notifier(db, server, key, now, sender=None):
        events.append("notify")
        return NotificationStats()

    service = SchedulerService(
        config(tmp_path),
        db=DB(),
        clock=clock,
        puller=puller,
        ingester=ingester,
        notifier=notifier,
    )
    service.tick()

    events.append("health" if service.read_health()["last_tick_at"] else "missing")
    assert events == ["recover", "pull", "ingest", "notify", "health"]


def test_pull_failure_does_not_block_recover_or_due_notification(tmp_path):
    """WebDAV 故障不得阻断已能执行的恢复和通知。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    now_ms = int(clock().timestamp() * 1000)
    sent = []

    def broken_pull(*args):
        raise OSError("secret URL and path")

    service = make_service(
        tmp_path,
        clock,
        puller=broken_pull,
        sender=lambda *args, **kwargs: sent.append(1) or Response(),
    )
    expired = service.db.enqueue({"conv_id": "u1", "mid": 1}, now_ms - 20_000)
    assert service.db.mark_ack_sent(expired, now_ms - 19_000)
    service.db.claim("cursor", now_ms - 20_000, 1, 1)
    due = service.db.enqueue({"conv_id": "u2", "mid": 2}, now_ms - 10_000)

    result = service.tick()

    assert service.db.get(expired)["status"] == "retry_wait"
    assert service.db.get(due)["ack_sent_at"] == now_ms
    assert result.errors == ("pull",)
    assert result.notifications.sent == 1
    assert sent == [1]


def test_ingest_failure_still_runs_notifications(tmp_path):
    """摄取失败仅隔离该阶段，不阻断 SLA 通知。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    now_ms = int(clock().timestamp() * 1000)
    service = make_service(
        tmp_path,
        clock,
        ingester=lambda *args: (_ for _ in ()).throw(RuntimeError("db")),
    )
    job_id = service.db.enqueue({"conv_id": "u1", "mid": 1}, now_ms - 10_000)

    result = service.tick()

    assert result.errors == ("ingest",)
    assert result.notifications.sent == 1
    assert service.db.get(job_id)["ack_sent_at"] == now_ms


def test_adaptive_pull_intervals_are_15_30_and_120_seconds(tmp_path):
    """消息后 15 秒，1-10 轮空闲 30 秒，超过 10 轮为 120 秒。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    counts = iter([1] + [0] * 11)
    service = make_service(
        tmp_path, clock, ingester=lambda *args: next(counts)
    )

    assert service.tick().next_poll_seconds == 15
    for _ in range(10):
        assert service.tick().next_poll_seconds == 30
    assert service.tick().next_poll_seconds == 120


def test_quiet_hours_use_300_seconds_but_still_notify(tmp_path):
    """quiet hours 只降低拉取频率，不关闭确认、入队或通知。"""

    clock = FakeClock(datetime(2026, 7, 10, 1))
    now_ms = int(clock().timestamp() * 1000)
    service = make_service(tmp_path, clock)
    job_id = service.db.enqueue({"conv_id": "u1", "mid": 1}, now_ms - 10_000)

    result = service.tick()

    assert result.next_poll_seconds == 300
    assert result.notifications.sent == 1
    assert service.db.get(job_id)["ack_sent_at"] == now_ms


def test_repeated_rounds_are_idempotent_and_webdav_is_mocked(tmp_path):
    """重复 WebDAV 拉取和摄取只创建一个持久任务。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    cfg = config(tmp_path)
    listing = """<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/share/conversations/u1.jsonl</d:href>
      <d:propstat><d:prop><d:getetag>"v1"</d:getetag>
      <d:resourcetype/></d:prop></d:propstat></d:response>
    </d:multistatus>"""
    record = '{"mid":1,"conv_id":"u1","direction":"in","content":"secret"}\n'

    with responses.RequestsMock() as mocked:
        mocked.add(
            "PROPFIND",
            "https://dav.example/share/conversations/",
            body=listing,
            status=207,
        )
        mocked.add(
            responses.GET,
            "https://dav.example/share/conversations/u1.jsonl",
            body=record,
            status=200,
            headers={"ETag": '"v1"'},
        )
        mocked.add(
            "PROPFIND",
            "https://dav.example/share/conversations/",
            body=listing,
            status=207,
        )
        service = SchedulerService(
            cfg,
            clock=clock,
            webdav_client=WebDAVClient(
                cfg.webdav_url, cfg.webdav_user, cfg.webdav_password
            ),
            sender=lambda *args, **kwargs: Response(),
        )
        first = service.tick()
        second = service.tick()

    assert first.new_jobs == 1
    assert second.new_jobs == 0
    assert service.db.find("u1", 1) is not None


def test_health_exposes_notification_stats_without_sensitive_values(tmp_path):
    """健康快照包含 notifier 统计但不包含凭据、URL、路径或 payload。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    now_ms = int(clock().timestamp() * 1000)
    service = make_service(tmp_path, clock)
    service.db.enqueue(
        {
            "conv_id": "u1",
            "mid": 1,
            "content": "payload https://private/?api_key=secret",
        },
        now_ms - 10_000,
    )

    service.tick()
    encoded = json.dumps(service.read_health(), ensure_ascii=False)

    assert service.read_health()["notifications"]["sent"] == 1
    for secret in ("api-key", "password", "private", str(tmp_path)):
        assert secret not in encoded


def test_run_forever_is_stoppable_and_never_busy_loops(tmp_path):
    """可注入 sleep，停止后退出且每次等待均为正数。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    sleeps = []
    service = make_service(tmp_path, clock)

    def sleeper(seconds):
        sleeps.append(seconds)
        service.stop()

    service.sleep = sleeper
    service.run_forever()

    assert len(sleeps) == 1
    assert sleeps[0] > 0


def test_run_forever_handles_keyboard_interrupt_cleanly(tmp_path):
    """CTRL+C 不向外传播，也不使进程进入忙循环。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    service = make_service(tmp_path, clock)
    service.sleep = lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt())

    service.run_forever()

    assert service.stopped


def test_status_counts_closes_every_connection_across_rounds(tmp_path, monkeypatch):
    """常驻多轮统计不得泄漏 SQLite 连接句柄。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    service = make_service(tmp_path, clock)
    opened = []
    closed = []
    real_connect = sqlite3.connect

    class Counting:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def __enter__(self):
            return self._real.__enter__()

        def __exit__(self, *args):
            return self._real.__exit__(*args)

        def close(self):
            closed.append(1)
            self._real.close()

    def tracking(*args, **kwargs):
        opened.append(1)
        return Counting(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", tracking)
    for _ in range(3):
        service.tick()

    assert opened
    assert len(opened) == len(closed)


def test_pull_failure_keeps_loop_at_next_poll_seconds(tmp_path):
    """pull 持续失败不得把循环拖到错误退避上限，仍按正常轮询节奏运行。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    sleeps = []
    service = make_service(
        tmp_path,
        clock,
        puller=lambda *args: (_ for _ in ()).throw(OSError("temporary")),
    )

    def sleeper(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 3:
            service.stop()

    service.sleep = sleeper
    service.run_forever()

    assert sleeps == [30, 30, 30]


def test_pull_circuit_breaker_backs_off_without_delaying_notifications(tmp_path):
    """pull 熔断退避只节流外部拉取，recover/notify 仍每轮及时执行。"""

    clock = FakeClock(datetime(2026, 7, 10, 9))
    cfg = config(tmp_path, retry_base_seconds=2, retry_max_seconds=8)
    attempts = []
    sent = []

    def failing_pull(*args):
        attempts.append(int(clock().timestamp() * 1000))
        raise OSError("temporary")

    service = SchedulerService(
        cfg,
        clock=clock,
        puller=failing_pull,
        ingester=lambda *args: 0,
        sender=lambda *args, **kwargs: sent.append(1) or Response(),
    )
    now_ms = int(clock().timestamp() * 1000)
    due = service.db.enqueue({"conv_id": "u1", "mid": 1}, now_ms - 10_000)

    first = service.tick()
    assert first.errors == ("pull",)
    assert service.db.get(due)["ack_sent_at"] == now_ms
    assert len(attempts) == 1

    second = service.tick()
    assert second.errors == ()
    assert len(attempts) == 1

    clock.advance(2)
    service.tick()
    assert len(attempts) == 2


def test_pid_lock_rejects_second_instance_and_recovers_stale_file(tmp_path):
    """活实例互斥，遗留的无效 PID 文件不阻断服务重启。"""

    path = tmp_path / "scheduler.lock"
    with PidFileLock(path):
        with pytest.raises(AlreadyRunningError):
            with PidFileLock(path):
                pass
    path.write_text("999999999", encoding="ascii")
    with PidFileLock(path):
        assert path.exists()
    assert not path.exists()


@pytest.mark.parametrize(
    "field,value",
    (
        ("webdav_url", "file:///private"),
        ("vocechat_server", ""),
        ("active_interval", 0),
        ("quiet_start", 24),
        ("retry_max_seconds", 0),
    ),
)
def test_configuration_is_strictly_validated(tmp_path, field, value):
    """非法 URL、空凭据和错误时间参数必须在启动前拒绝。"""

    with pytest.raises((TypeError, ValueError)):
        config(tmp_path, **{field: value})


def test_environment_config_accepts_planned_hh_mm_quiet_boundaries(tmp_path):
    """计划中的 00:00/07:00 配置应严格解析为整点边界。"""

    values = {
        "VOCECHAT_SERVER_URL": "https://chat.example",
        "VOCECHAT_API_KEY": "key",
        "WEBDAV_URL": "https://dav.example",
        "WEBDAV_USER": "user",
        "WEBDAV_PASSWORD": "password",
        "SCHEDULER_QUIET_START": "00:00",
        "SCHEDULER_QUIET_END": "07:00",
    }

    loaded = SchedulerConfig.from_mapping(values, tmp_path)

    assert loaded.quiet_start == 0
    assert loaded.quiet_end == 7


def test_cli_init_db_health_and_missing_runtime_config(tmp_path, capsys):
    """CLI 的本地命令无需凭据，run/once 则严格要求完整配置。"""

    db_path = tmp_path / "queue.db"
    health_path = tmp_path / "health.json"
    health_path.write_text('{"status":"ok"}', encoding="utf-8")

    assert scheduler_cli.main(
        ["--db", str(db_path), "--health", str(health_path), "init-db"],
        environ={},
    ) == 0
    assert db_path.exists()
    assert scheduler_cli.main(
        ["--db", str(db_path), "--health", str(health_path), "health"],
        environ={},
    ) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"status": "ok"}
    assert scheduler_cli.main(
        ["--db", str(db_path), "--health", str(health_path), "status"],
        environ={},
    ) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"status": "ok"}
    assert scheduler_cli.main(
        ["--db", str(db_path), "once"], environ={}
    ) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "config_error"
