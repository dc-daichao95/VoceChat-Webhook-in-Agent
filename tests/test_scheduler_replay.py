"""端到端回放"昨夜失败场景"的确定性验收测试。

复现 2026-07-09 夜间 mid=1431 的长延迟事故:消息落盘 -> 调度器发现入队 ->
10s 内 ack -> 45s 内 partial/status -> Cursor 卡死/离线期间持久排队 -> 恢复后
按 mid FIFO 幂等发送正式回复(只发一次) -> 重启后队列不丢。全部使用注入时钟、
mock WebDAV(puller 直接落盘)与 mock send,不真实等待、不联外网。
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.final_delivery import send_final
from scheduler.notifier import ACK_TEXT, STATUS_TEXT
from scheduler.outbox import Outbox
from scheduler.service import SchedulerConfig, SchedulerService

# 事故当晚的落盘时间;23 点不在 quiet(00:00-07:00)窗口,走活跃轮询节奏。
INCIDENT = datetime(2026, 7, 9, 23, 50, 49)
CHAT = "https://chat.example"
KEY = "api-key"


class FakeClock:
    """可推进且不依赖真实等待的确定性本地时钟。"""

    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class OkResponse:
    """通知与正式发送所需的最小成功响应。"""

    ok = True
    status_code = 200


def inbound(mid, conv_id="u2", content="查天气"):
    """构造一条归一化入站记录。"""
    return {"mid": mid, "conv_id": conv_id, "direction": "in", "content": content}


def config(tmp_path, **overrides):
    """构造不含真实凭据的完整调度器配置。"""
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
        "vocechat_server": CHAT,
        "vocechat_api_key": KEY,
    }
    values.update(overrides)
    return SchedulerConfig(**values)


def spool_puller(spool):
    """把内存 spool 落盘为 inbound JSONL,模拟 WebDAV 条件 GET 下载。"""

    def puller(client, remote, state, inbound_dir):
        base = Path(inbound_dir)
        base.mkdir(parents=True, exist_ok=True)
        for conv_id, records in spool.items():
            lines = "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in records
            )
            (base / (conv_id + ".jsonl")).write_text(lines, encoding="utf-8")
        return state

    return puller


def notif_sender(sends):
    """记录每次 SLA 通知的发送目标与文本。"""

    def sender(server, api_key, text, markdown=False, timeout=30, **target):
        sends.append((target, text))
        return OkResponse()

    return sender


def build_service(tmp_path, clock, spool, sends):
    """构造仅依赖注入 puller/sender、不访问网络的调度器服务。"""
    return SchedulerService(
        config(tmp_path),
        clock=clock,
        puller=spool_puller(spool),
        sender=notif_sender(sends),
    )


def _now_ms(clock):
    return int(clock().timestamp() * 1000)


def test_overnight_message_is_detected_enqueued_and_acked_within_10s(tmp_path):
    """落盘 -> 一轮内被发现入队;卡死 10s 后恰好确认一次。"""
    clock = FakeClock(INCIDENT)
    sends = []
    service = build_service(tmp_path, clock, {"u2": [inbound(1431)]}, sends)

    first = service.tick()

    assert first.new_jobs == 1
    job = service.db.find("u2", 1431)
    assert job is not None and job["status"] == "pending"
    assert sends == []  # 发现瞬间年龄为 0,尚不触发确认

    # Cursor 领取后卡死,不再处理。
    assert service.db.claim("cursor-stall", _now_ms(clock), 1, 120)
    clock.advance(seconds=10)
    result = service.tick()

    assert result.notifications.sent == 1
    assert sends == [({"uid": 2}, ACK_TEXT)]
    assert service.db.get(job["id"])["ack_sent_at"] is not None


def test_status_update_fires_once_at_45s_without_evidence(tmp_path):
    """无证据时 45s 发送一次状态说明,后续轮次不重复。"""
    clock = FakeClock(INCIDENT)
    sends = []
    service = build_service(tmp_path, clock, {"u2": [inbound(1431)]}, sends)
    service.tick()
    job_id = service.db.find("u2", 1431)["id"]
    service.db.claim("cursor-stall", _now_ms(clock), 1, 120)

    clock.advance(seconds=10)
    service.tick()  # 10s: ack
    clock.advance(seconds=35)
    service.tick()  # 45s: status(无证据)

    assert ({"uid": 2}, STATUS_TEXT) in sends
    assert service.db.get(job_id)["partial_sent_at"] is not None

    before = len(sends)
    clock.advance(seconds=60)
    service.tick()
    assert len(sends) == before  # ack/partial 均已发出,不再重复


def test_partial_uses_saved_evidence_at_45s(tmp_path):
    """已有结构化证据时,45s 用确定性模板发送部分结果。"""
    clock = FakeClock(INCIDENT)
    sends = []
    service = build_service(tmp_path, clock, {"u2": [inbound(1431)]}, sends)
    service.tick()
    job_id = service.db.find("u2", 1431)["id"]
    now = _now_ms(clock)
    service.db.claim("cursor-stall", now, 1, 120)
    assert ConsumerQueue(service.db).append_evidence_owned(
        job_id,
        {"source": "中央气象台", "title": "台风预警", "summary": "影响华东沿海"},
        "cursor-stall",
        now + 1,
    )

    clock.advance(seconds=45)
    service.tick()

    partials = [text for target, text in sends if "中央气象台" in text]
    assert len(partials) == 1
    assert "仍在补充" in partials[0]
    assert service.db.get(job_id)["partial_sent_at"] is not None


def test_queue_keeps_new_messages_while_cursor_is_stalled(tmp_path):
    """Cursor 卡死持有 u2 时,新消息持续入队但不被越序领取。"""
    clock = FakeClock(INCIDENT)
    spool = {"u2": [inbound(1431)]}
    sends = []
    service = build_service(tmp_path, clock, spool, sends)
    service.tick()
    service.db.claim("cursor-stall", _now_ms(clock), 1, 600)

    # 卡死期间陆续到达 1432/1433。
    spool["u2"].extend([inbound(1432), inbound(1433)])
    clock.advance(seconds=15)
    result = service.tick()

    assert result.new_jobs == 2
    for mid in (1432, 1433):
        assert service.db.find("u2", mid)["status"] == "pending"
    # 同一会话已有 processing 任务,其它 worker 不能抢占后续 mid。
    assert service.db.claim("cursor-online", _now_ms(clock), 3, 120) == []


def test_expired_lease_recovers_then_fifo_completes_each_final_once(tmp_path):
    """租约过期回队 -> 恢复后按 mid FIFO 完成 -> 每条正式回复只发一次。"""
    clock = FakeClock(INCIDENT)
    sends = []
    finals = []
    service = build_service(
        tmp_path, clock, {"u2": [inbound(m) for m in (1431, 1432, 1433)]}, sends
    )
    result = service.tick()
    assert result.new_jobs == 3
    ids = {mid: service.db.find("u2", mid)["id"] for mid in (1431, 1432, 1433)}

    # 卡死 worker 领取 1431(短租约)后失联。
    service.db.claim("cursor-stall", _now_ms(clock), 1, 30)
    clock.advance(seconds=40)
    recovered = service.tick()
    assert recovered.recovered == 1
    assert service.db.get(ids[1431])["status"] == "retry_wait"

    def final_sender(*args, **kwargs):
        finals.append((args, kwargs))
        return OkResponse()

    for expected_mid in (1431, 1432, 1433):
        now = _now_ms(clock)
        jobs = service.db.claim("cursor-online", now, 3, 120)
        assert [job["mid"] for job in jobs] == [expected_mid]
        outcome = send_final(
            service.db, jobs[0]["id"], "cursor-online",
            "reply-{}".format(expected_mid), False, CHAT, KEY,
            clock=lambda value=now: value, sender=final_sender,
            recorder=lambda record: None,
        )
        assert outcome["status"] == "done"
        clock.advance(seconds=1)

    # 幂等:对已完成任务再次发送被拒,sender 不再调用。
    calls_before = len(finals)
    blocked = send_final(
        service.db, ids[1431], "cursor-online", "dup", False, CHAT, KEY,
        clock=lambda: _now_ms(clock), sender=final_sender,
        recorder=lambda record: None,
    )
    assert blocked["status"] == "blocked"
    assert len(finals) == calls_before == 3

    # 重启:重新打开同一 DB,队列不丢,三条均为 done。
    reopened = QueueDB(service.config.db_path)
    for mid in (1431, 1432, 1433):
        assert reopened.find("u2", mid)["status"] == "done"


def test_scheduler_survives_webdav_failure_without_blocking_intake(tmp_path):
    """WebDAV/网络故障只隔离 pull 阶段,不崩溃、不阻塞已到期 SLA 通知。"""
    clock = FakeClock(INCIDENT)
    sends = []

    def broken_pull(*args):
        raise OSError("secret dav url and path")

    service = SchedulerService(
        config(tmp_path), clock=clock,
        puller=broken_pull, sender=notif_sender(sends),
    )
    job_id = service.db.enqueue(inbound(1431), _now_ms(clock) - 10_000)

    result = service.tick()

    assert result.errors == ("pull",)
    assert result.notifications.sent == 1
    assert service.db.get(job_id)["ack_sent_at"] is not None


def test_uncertain_final_is_frozen_and_never_resent(tmp_path):
    """发送结果未知冻结为 uncertain,永不自动重发,仅人工 reconcile 可解。"""
    clock = FakeClock(INCIDENT)
    sends = []
    service = build_service(tmp_path, clock, {"u2": [inbound(1431)]}, sends)
    service.tick()
    now = _now_ms(clock)
    job = service.db.claim("cursor-online", now, 1, 120)[0]
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("unknown result")

    outcome = send_final(
        service.db, job["id"], "cursor-online", "reply", False, CHAT, KEY,
        clock=lambda: now, sender=boom, recorder=lambda record: None,
    )
    blocked = send_final(
        service.db, job["id"], "cursor-online", "reply", False, CHAT, KEY,
        clock=lambda: now + 1, sender=boom, recorder=lambda record: None,
    )

    assert outcome["status"] == "uncertain"
    assert blocked["status"] == "blocked"
    assert calls == [1]
    assert Outbox(service.db).state(job["id"], "final")["state"] == "uncertain"
    # 人工核对 VoceChat 已送达后显式收敛。
    assert ConsumerQueue(service.db).reconcile(
        job["id"], "mark-done", now + 2
    ) == "done"
