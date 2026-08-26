"""TDD: DJ 社交喜好關聯、同歌手連播與情境擴充測試

測試範圍：
1. 歌曲與在場成員的喜好共鳴（Social Bridge：其他在場者也點過/按過讚/有感觸）
2. 歌手喜好關聯（Artist Affinity：點播者或在場者常聽該歌手）
3. 久別重逢提示（Long time no hear）
4. 同歌手連播（Back-to-Back）偵測
5. 星期與時段環境標籤合成
"""
from __future__ import annotations

import time
import tempfile
import pytest

from music_memory import MusicMemory
from dj_social_affinity import (
    find_song_social_affinity,
    detect_back_to_back_artist,
    format_temporal_atmosphere,
    assess_channel_heat,
)


def _make_mm(songs: dict) -> MusicMemory:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        mm = MusicMemory(tmp.name)
        mm._data = {"songs": songs, "recommendations": {}}
        return mm


def test_social_bridge_when_other_present_member_liked_song():
    """在場其他成員按過讚時，應能產生社交橋樑提示。"""
    songs = {
        "https://www.youtube.com/watch?v=song1": {
            "title": "晴天",
            "uploader": "周杰倫",
            "webpage_url": "https://www.youtube.com/watch?v=song1",
            "total_plays": 5,
            "plays": [{"by": "Alice", "ts": time.time() - 3600}],
            "requesters": {"Alice": 5},
            "likes": {"Bob": time.time() - 1800},
            "reactions": {},
        }
    }
    mm = _make_mm(songs)
    info = {"title": "晴天", "webpage_url": "https://www.youtube.com/watch?v=song1"}

    # Alice 點歌，Bob 在場且 Bob 按過讚
    affinity = find_song_social_affinity(
        mm, info, requester="Alice", present_members={"Alice", "Bob"}
    )
    assert affinity is not None
    assert "Bob" in affinity
    assert "讚" in affinity or "喜歡" in affinity


def test_social_bridge_when_other_present_member_also_requested():
    """在場其他成員之前也點播過這首歌時，應能產生社交提示。"""
    songs = {
        "https://www.youtube.com/watch?v=song2": {
            "title": "稻香",
            "uploader": "周杰倫",
            "webpage_url": "https://www.youtube.com/watch?v=song2",
            "total_plays": 8,
            "plays": [{"by": "Bob", "ts": time.time() - 3600}],
            "requesters": {"Bob": 3, "Alice": 5},
            "likes": {},
            "reactions": {},
        }
    }
    mm = _make_mm(songs)
    info = {"title": "稻香", "webpage_url": "https://www.youtube.com/watch?v=song2"}

    # Alice 點歌，Bob 在場（Bob 也常聽）
    affinity = find_song_social_affinity(
        mm, info, requester="Alice", present_members={"Alice", "Bob"}
    )
    assert affinity is not None
    assert "Bob" in affinity
    assert "常聽" in affinity or "點過" in affinity
    assert "次" not in affinity  # 不再包含生硬的次數計數


def test_artist_affinity_when_requester_frequently_plays_artist():
    """點播者經常點某位歌手時，應提示歌手偏好。"""
    songs = {
        f"url_{i}": {
            "title": f"周杰倫 - 歌曲{i}",
            "webpage_url": f"url_{i}",
            "total_plays": 1,
            "requesters": {"Alice": 1},
        }
        for i in range(4)
    }
    mm = _make_mm(songs)
    info = {"title": "周杰倫 - 夜曲", "webpage_url": "new_url"}

    # Alice 點了一首新歌，但 Alice 之前點過 4 首周杰倫的歌
    affinity = find_song_social_affinity(
        mm, info, requester="Alice", present_members={"Alice"}
    )
    assert affinity is not None
    assert "Alice" in affinity
    assert "周杰倫" in affinity


def test_long_time_no_play_recency_affinity():
    """若點播者上次聽這首歌已是 30 天前，應提示久別重逢。"""
    now = 1700000000.0
    songs = {
        "url_old": {
            "title": "十年",
            "uploader": "陳奕迅",
            "webpage_url": "url_old",
            "total_plays": 2,
            "plays": [{"by": "Alice", "ts": now - 40 * 86400}],
            "requesters": {"Alice": 2},
        }
    }
    mm = _make_mm(songs)
    info = {"title": "十年", "webpage_url": "url_old"}

    affinity = find_song_social_affinity(
        mm, info, requester="Alice", present_members={"Alice"}, now_ts=now
    )
    assert affinity is not None
    assert "十年" in affinity or "Alice" in affinity or "上次" in affinity or "久" in affinity


def test_detect_back_to_back_artist():
    """前後兩首為同一歌手時，應正確偵測連播。"""
    assert detect_back_to_back_artist("周杰倫 - 晴天", "周杰倫 - 稻香") == "周杰倫"
    assert detect_back_to_back_artist("Taylor Swift - Lover", "Taylor Swift - Cruel Summer") == "Taylor Swift"
    assert detect_back_to_back_artist("周杰倫 - 晴天", "五月天 - 溫柔") is None
    assert detect_back_to_back_artist("", "周杰倫 - 稻香") is None
    assert detect_back_to_back_artist("晴天", "周杰倫 - 晴天") is None  # 前者無歌手前綴


def test_format_temporal_atmosphere():
    """星期與時段合成環境標籤。"""
    import datetime
    dt = datetime.datetime(2026, 8, 21, 21, 30, 0)
    ts = dt.timestamp()

    env_str = format_temporal_atmosphere(city="台北", season="夏末", slot="深夜", ts=ts)
    assert "台北" in env_str
    assert "夏末" in env_str
    assert "週五" in env_str
    assert "深夜" in env_str


class _MockEntry:
    def __init__(self, ts: float):
        self.ts = ts


class _MockTracker:
    def __init__(self, entries: list[_MockEntry]):
        self._window = entries


def test_assess_channel_heat_solo():
    """只有 1 人在線時，應進入 solo 親密模式。"""
    mode, instr = assess_channel_heat(tracker=None, conv_buffer=None, n_online=1)
    assert mode == "solo"
    assert "親密" in instr or "老朋友" in instr


def test_assess_channel_heat_quiet_group():
    """5 人在線但近 3 分鐘完全無發言時，應判定為 quiet_group 沉浸聽歌/工作模式。"""
    now = 1000.0
    # 發言都在 10 分鐘前
    old_entries = [_MockEntry(ts=now - 500), _MockEntry(ts=now - 400)]
    tracker = _MockTracker(old_entries)
    
    mode, instr = assess_channel_heat(tracker=tracker, conv_buffer=None, n_online=5, now_ts=now)
    assert mode == "quiet_group"
    assert "安靜聽歌" in instr or "音樂故事" in instr or "陪伴感" in instr


def test_assess_channel_heat_active_chat():
    """3 人在線且近 2 分鐘內有 5 次密集發言，應判定為 active_chat 熱聊模式。"""
    now = 1000.0
    recent_entries = [
        _MockEntry(ts=now - 10),
        _MockEntry(ts=now - 25),
        _MockEntry(ts=now - 40),
        _MockEntry(ts=now - 60),
    ]
    tracker = _MockTracker(recent_entries)
    
    mode, instr = assess_channel_heat(tracker=tracker, conv_buffer=None, n_online=3, now_ts=now)
    assert mode == "active_chat"
    assert "熱烈" in instr or "精簡" in instr
