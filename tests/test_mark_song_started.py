"""TDD — _mark_song_started：一首歌真正開播的單一簽收點（第 1 刀 skip 訊號）。

歸零 skip 旗標、戳開播時間戳、補推 HUD 快照、把 info 上暫存的口白歸屬
（_narration_id/_narration_mode，見 music_cog_tail_dj._attach_narration）簽收，
並記一筆 song_plays.jsonl。只驗這個函式本身，不驗算 _stream_loop 其餘邏輯。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import cogs.music_cog_subsystem as subsystem


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.music_memory = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def _patch_log(monkeypatch):
    rows = []
    monkeypatch.setattr(subsystem, "log_song_play", lambda rec: rows.append(rec))
    return rows


def test_mark_song_started_returns_start_time_and_resets_skip_flag(monkeypatch):
    rows = _patch_log(monkeypatch)
    cog = _make_cog()
    cog._current_song_skipped = True
    cog._republish_queue_snapshot = MagicMock()
    info = {"webpage_url": "https://youtu.be/dQw4w9WgXcQ"}

    start = cog._mark_song_started(info)

    assert start == cog._current_stream_start_time
    assert cog._current_song_skipped is False
    cog._republish_queue_snapshot.assert_called_once()
    assert len(rows) == 1


def test_mark_song_started_pops_narration_and_sets_current_narration(monkeypatch):
    rows = _patch_log(monkeypatch)
    cog = _make_cog()
    cog._republish_queue_snapshot = MagicMock()
    info = {
        "webpage_url": "https://youtu.be/dQw4w9WgXcQ",
        "_narration_id": "nid1", "_narration_mode": "life",
        "requested_by": "大肚",
    }

    cog._mark_song_started(info)

    assert "_narration_id" not in info
    assert "_narration_mode" not in info
    assert cog._current_narration == ("nid1", "life")
    assert cog._current_play_info is info
    assert len(rows) == 1
    rec = rows[0]
    assert rec["type"] == "play"
    assert rec["play_id"] == cog._current_play_id
    assert rec["narration_id"] == "nid1"
    assert rec["mode"] == "life"
    assert rec["video_id"] == "dQw4w9WgXcQ"
    assert rec["requested_by"] == "大肚"


def test_mark_song_started_no_narration_logs_none(monkeypatch):
    rows = _patch_log(monkeypatch)
    cog = _make_cog()
    cog._republish_queue_snapshot = MagicMock()
    info = {"webpage_url": "https://youtu.be/dQw4w9WgXcQ"}

    cog._mark_song_started(info)

    assert cog._current_narration == (None, None)
    rec = rows[0]
    assert rec["narration_id"] is None
    assert rec["mode"] is None


def test_mark_song_started_replay_gets_fresh_play_id_and_no_stale_narration(monkeypatch):
    """模擬 replay_agent 淺拷貝 info（同一首重播）：第二次開播要有新的 play_id，
    且因為 info2 是新 dict（沒有舊的 _narration_id），narration 為 None。"""
    rows = _patch_log(monkeypatch)
    cog = _make_cog()
    cog._republish_queue_snapshot = MagicMock()
    info = {
        "webpage_url": "https://youtu.be/dQw4w9WgXcQ",
        "_narration_id": "nid1", "_narration_mode": "life",
    }
    cog._mark_song_started(info)
    first_play_id = cog._current_play_id

    info2 = dict(info)  # 已經被第一次 pop 掉 _narration_id/_narration_mode
    cog._mark_song_started(info2)

    assert cog._current_play_id != first_play_id
    assert cog._current_play_info is info2
    assert rows[-1]["narration_id"] is None
    assert rows[-1]["play_id"] == cog._current_play_id
