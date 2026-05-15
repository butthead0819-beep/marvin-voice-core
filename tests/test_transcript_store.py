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


# ── 新增：guild-wide 查詢 + minutes 單位 ──────────────────────────────────────

def test_get_recent_guild_wide_returns_all_speakers(store):
    """speaker=None 時，同一個 guild 的所有說話者記錄都要回傳"""
    ts = time.time()
    store.save(speaker="Alice", guild_id=999, text="Alice 說話", timestamp=ts)
    store.save(speaker="Bob", guild_id=999, text="Bob 說話", timestamp=ts)
    store.save(speaker="Carol", guild_id=999, text="Carol 說話", timestamp=ts)
    # 不同 guild，不應出現
    store.save(speaker="Alice", guild_id=888, text="別的 guild", timestamp=ts)

    rows = store.get_recent(speaker=None, guild_id=999, minutes=5)
    texts = [r["text"] for r in rows]
    assert len(rows) == 3
    assert "Alice 說話" in texts
    assert "Bob 說話" in texts
    assert "Carol 說話" in texts


def test_get_recent_minutes_overrides_days(store):
    """minutes 參數有值時，以 minutes 為準，忽略 days"""
    now = time.time()
    recent_ts = now - 60        # 1 分鐘前
    old_ts = now - 86400 * 10   # 10 天前

    store.save(speaker="Jack", guild_id=123, text="最近的", timestamp=recent_ts)
    store.save(speaker="Jack", guild_id=123, text="很舊的", timestamp=old_ts)

    rows = store.get_recent(speaker=None, guild_id=123, minutes=5)
    assert len(rows) == 1
    assert rows[0]["text"] == "最近的"


def test_get_recent_backward_compat_original_signature(store):
    """舊有的位置參數呼叫方式仍正常運作"""
    ts = time.time()
    store.save(speaker="Jack", guild_id=123, text="位置參數測試", timestamp=ts)

    # 原始呼叫方式：get_recent(speaker, guild_id)
    rows = store.get_recent("Jack", 123)
    assert len(rows) == 1
    assert rows[0]["text"] == "位置參數測試"


def test_get_recent_speaker_none_guild_wide(store):
    """speaker=None 時，不加 speaker 過濾，回傳 guild 全部記錄"""
    ts = time.time()
    store.save(speaker="Alice", guild_id=777, text="A 的話", timestamp=ts)
    store.save(speaker="Bob", guild_id=777, text="B 的話", timestamp=ts)

    rows = store.get_recent(speaker=None, guild_id=777, days=1)
    assert len(rows) == 2
    speakers = {r["speaker"] for r in rows}
    assert speakers == {"Alice", "Bob"}
