"""
tests/test_music_cog_now_playing_publish.py
TDD：MusicCog._publish_now_playing_state — 換歌/停播時寫跨進程橋接檔。

main_satellite.py 的 /now（HUD 用）讀不到真 Discord bot 的播放狀態，因為兩個進程
各自獨立的 MusicCog 從不互通（見 now_playing_state.py docstring）。這裡驗證 MusicCog
換歌時把 title/by/cover/palette 正確寫出去，停播時 playing=False；寫檔失敗不该炸掉
呼叫端（優雅降級，見 CLAUDE.md 音訊播放安全原則的同一精神）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clear_away_mode_env(monkeypatch):
    """main_discord.py 模組層 load_dotenv() 可能把真實 .env 的 MARVIN_CAR_MODE 等帶進
    行程環境；這裡測的是「家用模式」預設行為，跟真實 .env 內容無關，先清乾淨。"""
    monkeypatch.delenv("MARVIN_SATELLITE_BROWSER", raising=False)
    monkeypatch.delenv("MARVIN_CAR_MODE", raising=False)


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.music_memory = MagicMock()

    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def test_publish_with_info_writes_playing_true(monkeypatch):
    import now_playing_state
    calls = []
    monkeypatch.setattr(now_playing_state, "save_now_playing_state",
                         lambda **kw: calls.append(kw))
    cog = _make_cog()
    cog.stream_queue = [{"title": "晴天", "requested_by": "小明", "url": "x",
                          "thumbnail": "http://x/next.jpg"}]
    cog._publish_now_playing_state({
        "title": "夜曲", "requested_by": "大肚",
        "thumbnail": "http://x/y.jpg", "palette": ["#111111"],
    })
    assert len(calls) == 1
    assert calls[0] == {
        "playing": True, "title": "夜曲", "by": "大肚",
        "cover": "http://x/y.jpg", "palette": ["#111111"],
        "queue": [{"title": "晴天", "by": "小明", "thumbnail": "http://x/next.jpg"}],
        "duration": None, "song_start_time": None, "comment": None,
    }


def test_publish_with_info_includes_duration_start_time_and_comment(monkeypatch):
    """HUD 黑膠展開的進度條/DJ 銳評要靠這三個欄位。"""
    import now_playing_state
    calls = []
    monkeypatch.setattr(now_playing_state, "save_now_playing_state",
                         lambda **kw: calls.append(kw))
    cog = _make_cog()
    cog._current_stream_start_time = 1700000000.0
    cog._current_stream_comment = "這首不錯。"
    cog._publish_now_playing_state({
        "title": "夜曲", "requested_by": "大肚", "duration": 245.0,
    })
    assert calls[0]["duration"] == 245.0
    assert calls[0]["song_start_time"] == 1700000000.0
    assert calls[0]["comment"] == "這首不錯。"


def test_publish_without_info_writes_playing_false(monkeypatch):
    import now_playing_state
    calls = []
    monkeypatch.setattr(now_playing_state, "save_now_playing_state",
                         lambda **kw: calls.append(kw))
    cog = _make_cog()
    cog._publish_now_playing_state(None)
    assert calls == [{"playing": False}]


def test_publish_swallows_write_failure(monkeypatch):
    import now_playing_state

    def _boom(**kw):
        raise OSError("disk full")
    monkeypatch.setattr(now_playing_state, "save_now_playing_state", _boom)
    cog = _make_cog()
    cog._publish_now_playing_state({"title": "夜曲", "requested_by": "大肚"})   # 不應丟例外


def test_republish_queue_snapshot_writes_current_queue(monkeypatch):
    """佇列變動（補歌/新點歌）後不必等下一首開播，立刻重寫橋接檔的 queue。"""
    import now_playing_state
    calls = []
    monkeypatch.setattr(now_playing_state, "save_now_playing_state",
                         lambda **kw: calls.append(kw))
    cog = _make_cog()
    cog._current_stream_info = {"title": "夜曲", "requested_by": "大肚",
                                 "thumbnail": "http://x/y.jpg", "palette": ["#111111"]}
    cog.stream_queue = [{"title": "晴天", "requested_by": "小明", "thumbnail": "http://x/n.jpg"}]
    cog._republish_queue_snapshot()
    assert calls[-1]["queue"] == [{"title": "晴天", "by": "小明", "thumbnail": "http://x/n.jpg"}]


def test_publish_skips_write_in_browser_satellite_mode(monkeypatch):
    """瀏覽器 satellite（在外）不該寫跨進程橋接檔，免得蓋掉家用 HUD 的 Pi/Discord 狀態。"""
    import now_playing_state
    calls = []
    monkeypatch.setattr(now_playing_state, "save_now_playing_state",
                         lambda **kw: calls.append(kw))
    monkeypatch.setenv("MARVIN_SATELLITE_BROWSER", "1")
    cog = _make_cog()
    cog._publish_now_playing_state({"title": "在外播放", "requested_by": "手機"})
    assert calls == []


def test_publish_skips_write_in_car_mode(monkeypatch):
    """車載 ESP32 puck（在外）不該寫跨進程橋接檔，理由同瀏覽器 satellite。"""
    import now_playing_state
    calls = []
    monkeypatch.setattr(now_playing_state, "save_now_playing_state",
                         lambda **kw: calls.append(kw))
    monkeypatch.setenv("MARVIN_CAR_MODE", "1")
    cog = _make_cog()
    cog._publish_now_playing_state({"title": "車上播放", "requested_by": "車上"})
    assert calls == []


def test_republish_queue_snapshot_writes_playing_false_when_idle(monkeypatch):
    """沒歌在播時佇列變動（例如收掉個人歌單殘留位）→ 橋接檔正確反映 playing=False。"""
    import now_playing_state
    calls = []
    monkeypatch.setattr(now_playing_state, "save_now_playing_state",
                         lambda **kw: calls.append(kw))
    cog = _make_cog()
    cog._current_stream_info = None
    cog._republish_queue_snapshot()
    assert calls == [{"playing": False}]
