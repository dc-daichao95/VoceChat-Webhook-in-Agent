"""在线 JSON 非有限数值与持久化序列化边界测试。"""

import json

import pytest
import responses

from scheduler.consumer import ConsumerQueue
from scheduler.db import QueueDB
from scheduler.online import OnlineFetchError, fetch_json


PUBLIC_IP = "93.184.216.34"


def public_resolver(hostname, port):
    """为 responses 测试返回固定公网地址。"""
    return [PUBLIC_IP]


def public_peer(response):
    """为无 socket 的 responses 响应返回固定 peer。"""
    return PUBLIC_IP


@responses.activate
@pytest.mark.parametrize(
    "body",
    (
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"outer":[{"value":1e400}]}',
    ),
)
def test_fetch_json_rejects_recursive_non_finite_numbers(body):
    """JSON 常量及溢出得到的递归非有限 float 都必须拒绝。"""
    url = "https://example.com/value.json"
    responses.add(
        responses.GET,
        url,
        body=body,
        content_type="application/json",
        status=200,
    )

    with pytest.raises(OnlineFetchError) as error:
        fetch_json(
            url,
            timeout=5,
            resolver=public_resolver,
            peer_getter=public_peer,
        )

    assert str(error.value) == "invalid JSON response"
    assert error.value.__cause__ is None


@responses.activate
def test_fetch_json_result_is_strictly_serializable():
    """成功 JSON 证据必须能由 allow_nan=False 严格序列化。"""
    url = "https://example.com/value.json"
    responses.add(
        responses.GET,
        url,
        json={"outer": [{"value": 1.25}]},
        status=200,
    )

    result = fetch_json(
        url,
        timeout=5,
        resolver=public_resolver,
        peer_getter=public_peer,
    )

    encoded = json.dumps(result, allow_nan=False)
    assert '"value": 1.25' in encoded


def test_queue_db_rejects_non_finite_evidence_before_persistence(tmp_path):
    """DB evidence_json 边界不得写入 Python 扩展 NaN。"""
    db = QueueDB(tmp_path / "queue.db")
    job_id = db.enqueue(
        {"conv_id": "u1", "mid": 1, "content": "weather"},
        detected_at=100,
    )
    assert db.claim("owner", 100, 1, 10)

    with pytest.raises(ValueError):
        ConsumerQueue(db).append_evidence_owned(
            job_id,
            {"kind": "json", "data": {"value": float("nan")}},
            "owner",
            now=101,
        )

    assert db.get(job_id)["evidence"] == []
