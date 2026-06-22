"""TranscriptStore.laugh_events：笑聲當下發聲/在場快照的存取。"""
from transcript_store import TranscriptStore


def _store():
    return TranscriptStore(db_path=":memory:")


def test_save_and_get_laugh_event_roundtrip():
    s = _store()
    s.save_laugh_event("showay", guild_id=1, channel_id=9, timestamp=100.0,
                       vocalizers=3, present=5)
    evs = s.get_laugh_events(guild_id=1, since=0, until=200)
    assert len(evs) == 1
    e = evs[0]
    assert (e["speaker"], e["vocalizers"], e["present"]) == ("showay", 3, 5)
    assert e["timestamp"] == 100.0


def test_get_laugh_events_filters_window_and_guild():
    s = _store()
    s.save_laugh_event("a", 1, 9, 50.0, 2, 3)    # 太早
    s.save_laugh_event("b", 1, 9, 150.0, 2, 3)   # 窗內
    s.save_laugh_event("c", 1, 9, 999.0, 2, 3)   # 太晚
    s.save_laugh_event("d", 2, 9, 150.0, 2, 3)   # 別的 guild
    evs = s.get_laugh_events(guild_id=1, since=100, until=200)
    assert [e["speaker"] for e in evs] == ["b"]
