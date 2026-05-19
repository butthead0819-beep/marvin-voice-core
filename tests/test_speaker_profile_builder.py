"""TDD：SpeakerProfileBuilder — 把分散在 5 個 store 的 speaker 訊號組成
SpeakerProfile，餵給 SemanticResolver。

5/21 vertical slice Step 4：純讀取 builder，零 prod 影響。
所有 stores 皆為 dependency injection；缺哪個 store → 對應欄位 None。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intent_agents.profile_builder import SpeakerProfileBuilder
from intent_agents.semantic_resolver import SpeakerProfile


# ── 1. Empty case ─────────────────────────────────────────────────────────

def test_no_stores_returns_minimal_profile():
    """全部 store 都 None → 只有 speaker 欄位有值。"""
    builder = SpeakerProfileBuilder()
    p = builder.build("大肚")
    assert isinstance(p, SpeakerProfile)
    assert p.speaker == "大肚"
    assert p.age is None
    assert p.birth_year is None
    assert p.recent_played == []
    assert p.time_of_day is None
    assert p.current_mood is None
    assert p.who_else_in_channel == []


# ── 2. Suki: birth_year + age ─────────────────────────────────────────────

def test_birth_year_extracted_from_suki_impression():
    """suki_impression 含 '1990 年出生' / 'b. 1990' / '1990年' → birth_year=1990。"""
    suki = MagicMock()
    suki.has_player = MagicMock(return_value=True)
    suki.get_player_memory = MagicMock(return_value={
        "suki_impression": "大肚是 1990 年出生的台灣人，喜歡周杰倫",
    })
    # clock 給 2026 年的 timestamp（便於 age 計算）
    builder = SpeakerProfileBuilder(suki=suki, clock=lambda: _ts_for_year(2026))
    p = builder.build("大肚")
    assert p.birth_year == 1990
    assert p.age == 36  # 2026 - 1990


def test_age_none_when_no_birth_year_in_impression():
    """impression 無年份字串 → birth_year 與 age 都是 None。"""
    suki = MagicMock()
    suki.has_player = MagicMock(return_value=True)
    suki.get_player_memory = MagicMock(return_value={
        "suki_impression": "喜歡音樂的玩家",
    })
    builder = SpeakerProfileBuilder(suki=suki, clock=lambda: _ts_for_year(2026))
    p = builder.build("大肚")
    assert p.birth_year is None
    assert p.age is None


def test_unknown_speaker_in_suki_returns_minimal():
    """suki.has_player(speaker)=False → 跳過 Suki 取值，不爆。"""
    suki = MagicMock()
    suki.has_player = MagicMock(return_value=False)
    builder = SpeakerProfileBuilder(suki=suki)
    p = builder.build("未知玩家")
    assert p.birth_year is None
    suki.get_player_memory.assert_not_called()


# ── 3. MusicMemory: recent_played ─────────────────────────────────────────

def test_recent_played_from_music_memory_titles_in_order():
    """get_top_songs_for_user → 取 title 字串組成 recent_played。"""
    music = MagicMock()
    music.get_top_songs_for_user = MagicMock(return_value=[
        {"title": "稻香", "uploader": "周杰倫"},
        {"title": "江南", "uploader": "林俊傑"},
        {"title": "倔強", "uploader": "五月天"},
    ])
    builder = SpeakerProfileBuilder(music=music)
    p = builder.build("大肚")
    assert p.recent_played == ["稻香", "江南", "倔強"]
    music.get_top_songs_for_user.assert_called_once_with("大肚", limit=10)


def test_recent_played_empty_when_user_has_no_history():
    music = MagicMock()
    music.get_top_songs_for_user = MagicMock(return_value=[])
    builder = SpeakerProfileBuilder(music=music)
    p = builder.build("新用戶")
    assert p.recent_played == []


def test_recent_played_skips_empty_titles():
    """資料破損的 entry (title 空) 不該滲漏到 recent_played。"""
    music = MagicMock()
    music.get_top_songs_for_user = MagicMock(return_value=[
        {"title": "稻香", "uploader": "周杰倫"},
        {"title": "", "uploader": "?"},  # corrupted
        {"uploader": "no title"},        # missing key
        {"title": "江南", "uploader": "林俊傑"},
    ])
    builder = SpeakerProfileBuilder(music=music)
    p = builder.build("大肚")
    assert p.recent_played == ["稻香", "江南"]


# ── 4. Temperature → mood ─────────────────────────────────────────────────

@pytest.mark.parametrize("level,expected_mood", [
    ("cold", "reflective"),
    ("warm", "relaxed"),
    ("hot", "energetic"),
])
def test_mood_mapped_from_temperature_level(level, expected_mood):
    """temperature.level → current_mood 用固定 mapping。"""
    monitor = MagicMock()
    monitor.level = level
    builder = SpeakerProfileBuilder(temperature=monitor)
    p = builder.build("大肚")
    assert p.current_mood == expected_mood


def test_mood_unknown_level_falls_to_none():
    """temperature.level 是其他字串 → current_mood=None，不亂塞。"""
    monitor = MagicMock()
    monitor.level = "lukewarm_typo"
    builder = SpeakerProfileBuilder(temperature=monitor)
    p = builder.build("大肚")
    assert p.current_mood is None


# ── 5. Clock → time_of_day ────────────────────────────────────────────────

@pytest.mark.parametrize("hour,expected", [
    (3,  "late_night"),
    (7,  "morning"),
    (10, "morning"),
    (14, "afternoon"),
    (19, "evening"),
    (23, "late_night"),
])
def test_time_of_day_from_clock_hour(hour, expected):
    builder = SpeakerProfileBuilder(clock=lambda: _ts_for_hour(hour))
    p = builder.build("大肚")
    assert p.time_of_day == expected


# ── 6. Channel members ────────────────────────────────────────────────────

def test_who_else_in_channel_excludes_self():
    """channel_members_provider 回傳全部成員 → builder 必須排除 speaker 自己。"""
    builder = SpeakerProfileBuilder(
        channel_members_provider=lambda: ["大肚", "露", "馬文"],
    )
    p = builder.build("大肚")
    assert p.who_else_in_channel == ["露", "馬文"]


def test_alone_in_channel_returns_empty_list():
    builder = SpeakerProfileBuilder(
        channel_members_provider=lambda: ["大肚"],
    )
    p = builder.build("大肚")
    assert p.who_else_in_channel == []


# ── 7. Graceful degradation under partial stores ─────────────────────────

def test_partial_stores_compose_without_error():
    """只給 music + temperature，沒給 suki / clock / channel → 不該炸。"""
    music = MagicMock()
    music.get_top_songs_for_user = MagicMock(return_value=[
        {"title": "稻香", "uploader": "周杰倫"},
    ])
    temp = MagicMock()
    temp.level = "warm"
    builder = SpeakerProfileBuilder(music=music, temperature=temp)
    p = builder.build("大肚")
    assert p.speaker == "大肚"
    assert p.recent_played == ["稻香"]
    assert p.current_mood == "relaxed"
    assert p.age is None
    assert p.who_else_in_channel == []


def test_store_exception_does_not_break_build():
    """單一 store 拋例外 → builder 跳過該欄位，其他照常組裝。"""
    music = MagicMock()
    music.get_top_songs_for_user = MagicMock(side_effect=RuntimeError("DB locked"))
    temp = MagicMock()
    temp.level = "hot"
    builder = SpeakerProfileBuilder(music=music, temperature=temp)
    p = builder.build("大肚")
    assert p.recent_played == []   # 失敗 → empty
    assert p.current_mood == "energetic"  # 其他欄位照樣填


# ── Helpers ───────────────────────────────────────────────────────────────

def _ts_for_year(year: int) -> float:
    """Return a Unix timestamp inside the given year (Jan 1 noon local time)."""
    import datetime
    return datetime.datetime(year, 1, 1, 12, 0, 0).timestamp()


def _ts_for_hour(hour: int) -> float:
    """Return a Unix timestamp at the given local hour on Jan 1."""
    import datetime
    return datetime.datetime(2026, 1, 1, hour, 30, 0).timestamp()
