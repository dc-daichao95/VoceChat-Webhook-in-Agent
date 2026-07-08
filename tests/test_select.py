# tests/test_select.py
from brain import select


def _rec(mid, direction="in"):
    return {"mid": mid, "conv_id": "u1", "direction": direction}


def test_select_skips_out_and_processed():
    records = [_rec(1), _rec(2, "out"), _rec(3), _rec(4)]
    out = select.select_pending("u1", records, last_processed_mid=2, seen_mids=set())
    assert [r["mid"] for r in out] == [3, 4]


def test_select_skips_seen_and_none_mid():
    records = [_rec(3), _rec(None), _rec(5)]
    out = select.select_pending("u1", records, last_processed_mid=0, seen_mids={3})
    assert [r["mid"] for r in out] == [5]


def test_select_sorted_ascending():
    records = [_rec(9), _rec(7), _rec(8)]
    out = select.select_pending("u1", records, last_processed_mid=0, seen_mids=set())
    assert [r["mid"] for r in out] == [7, 8, 9]
