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
    }


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
