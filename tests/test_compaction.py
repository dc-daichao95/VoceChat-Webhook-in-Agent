from brain import compaction


def test_split_recent_keeps_last_n():
    recs = [{"mid": i} for i in range(50)]
    old, recent = compaction.split_recent(recs, keep=20)
    assert len(recent) == 20 and recent[0]["mid"] == 30
    assert len(old) == 30 and old[-1]["mid"] == 29


def test_split_recent_under_keep_returns_no_old():
    recs = [{"mid": i} for i in range(5)]
    old, recent = compaction.split_recent(recs, keep=20)
    assert old == [] and len(recent) == 5


def test_needs_compaction():
    assert compaction.needs_compaction(41, trigger=40) is True
    assert compaction.needs_compaction(40, trigger=40) is False
