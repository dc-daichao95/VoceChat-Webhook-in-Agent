# tests/test_filters.py
from app import filters


def _dm(mid=1, uid=7910, ctype="text/plain", typ="normal", content="hi"):
    return {"mid": mid, "from_uid": uid, "created_at": 100,
            "detail": {"type": typ, "content_type": ctype, "content": content, "properties": None},
            "target": {"uid": 999}}


def _group(mid=2, uid=7910, gid=2, mentions=None, content="hi"):
    props = {"mentions": mentions} if mentions is not None else None
    return {"mid": mid, "from_uid": uid, "created_at": 100,
            "detail": {"type": "normal", "content_type": "text/plain", "content": content, "properties": props},
            "target": {"gid": gid}}


def test_is_normal_text():
    assert filters.is_normal_text(_dm()) is True
    assert filters.is_normal_text(_dm(typ="edit")) is False
    assert filters.is_normal_text(_dm(ctype="vocechat/file")) is False


def test_conv_id_of():
    assert filters.conv_id_of(_dm(uid=7910)) == "u7910"
    assert filters.conv_id_of(_group(gid=2)) == "g2"
    assert filters.conv_id_of({"target": {}}) is None


def test_should_accept_dm():
    assert filters.should_accept(_dm(), bot_uid=0, scope_dm=True, scope_group_mention=True) is True
    assert filters.should_accept(_dm(), bot_uid=0, scope_dm=False, scope_group_mention=True) is False


def test_should_accept_own_message_rejected():
    assert filters.should_accept(_dm(uid=0), bot_uid=0, scope_dm=True, scope_group_mention=True) is False


def test_should_accept_group_requires_mention():
    assert filters.should_accept(_group(mentions=[0]), bot_uid=0, scope_dm=True, scope_group_mention=True) is True
    assert filters.should_accept(_group(mentions=[5]), bot_uid=0, scope_dm=True, scope_group_mention=True) is False
    assert filters.should_accept(_group(mentions=None), bot_uid=0, scope_dm=True, scope_group_mention=True) is False


def test_build_in_record():
    rec = filters.build_in_record(_group(mid=2, uid=7910, gid=2, mentions=[0]), bot_uid=0)
    assert rec["mid"] == 2 and rec["conv_id"] == "g2" and rec["direction"] == "in"
    assert rec["mentioned_bot"] is True and rec["content"] == "hi"
    assert isinstance(rec["recorded_at"], int)
