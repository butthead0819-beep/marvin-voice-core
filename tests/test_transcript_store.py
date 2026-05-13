import time

import pytest

from transcript_store import TranscriptStore


@pytest.fixture
def store():
    return TranscriptStore(db_path=":memory:")


def test_save_and_get_recent(store):
    store.save(speaker="Jack", guild_id=123, text="哈囉馬文", timestamp=time.time())
    rows = store.get_recent(speaker="Jack", guild_id=123)
    assert len(rows) == 1
    assert rows[0]["speaker"] == "Jack"
    assert rows[0]["text"] == "哈囉馬文"
    assert "timestamp" in rows[0]


def test_get_recent_filters_by_days(store):
    old_ts = time.time() - 86400 * 10  # 10 天前
    recent_ts = time.time()
    store.save(speaker="Jack", guild_id=123, text="舊的", timestamp=old_ts)
    store.save(speaker="Jack", guild_id=123, text="新的", timestamp=recent_ts)
    rows = store.get_recent(speaker="Jack", guild_id=123, days=7)
    assert len(rows) == 1
    assert rows[0]["text"] == "新的"


def test_get_speakers_returns_unique(store):
    ts = time.time()
    store.save(speaker="Alice", guild_id=999, text="測試一", timestamp=ts)
    store.save(speaker="Alice", guild_id=999, text="測試二", timestamp=ts)
    store.save(speaker="Bob", guild_id=999, text="嗨", timestamp=ts)
    store.save(speaker="Bob", guild_id=777, text="不同 guild", timestamp=ts)
    speakers = store.get_speakers(guild_id=999)
    assert sorted(speakers) == ["Alice", "Bob"]


def test_save_empty_text_is_rejected(store):
    store.save(speaker="Jack", guild_id=123, text="", timestamp=time.time())
    rows = store.get_recent(speaker="Jack", guild_id=123)
    assert rows == []
