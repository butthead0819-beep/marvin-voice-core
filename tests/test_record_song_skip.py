"""TDD: skip 時把當前歌 video-id 記入持久化排除集的接線（2026-06-14）。

兩條 skip 路徑（IBA-T0 / PlaybackControlAgent）共用 VoiceController._record_song_skip。
這裡只驗 controller 端接線：拿當前歌 → 寫進 music_memory；fail-open 不炸。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.music_memory = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def test_record_song_skip_persists_current_video_id(tmp_path):
    from music_memory import MusicMemory
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    cog = _make_cog()
    cog.bot.music_memory = mm
    cog._current_stream_info = {"webpage_url": "https://youtu.be/dQw4w9WgXcQ", "url": "x"}

    cog._record_song_skip()

    assert "dQw4w9WgXcQ" in mm.get_skipped_video_ids()


def test_record_song_skip_also_records_artist(tmp_path):
    """Step 3：skip 同時記藝人級（供 explore retreat）。"""
    from music_memory import MusicMemory
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    cog = _make_cog()
    cog.bot.music_memory = mm
    cog._current_stream_info = {
        "title": "周杰倫 Jay Chou【晴天】-Official MV",
        "webpage_url": "https://youtu.be/dQw4w9WgXcQ", "url": "x",
    }
    cog._record_song_skip()
    assert "dQw4w9WgXcQ" in mm._data.get("artist_skips", {}).get("周杰倫", [])


def test_record_song_skip_noop_when_no_current_song(tmp_path):
    from music_memory import MusicMemory
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    cog = _make_cog()
    cog.bot.music_memory = mm
    cog._current_stream_info = None

    cog._record_song_skip()  # 不該炸

    assert mm.get_skipped_video_ids() == set()


def test_record_song_skip_noop_when_no_music_memory():
    cog = _make_cog()
    cog.bot.music_memory = None
    cog._current_stream_info = {"webpage_url": "https://youtu.be/dQw4w9WgXcQ"}

    cog._record_song_skip()  # fail-open，不丟例外


# ── 第 1 刀 skip 訊號：_log_song_skip 帶 play_id/narration_id/elapsed_s ──────

def test_record_song_skip_logs_play_id_and_elapsed_when_same_play(tmp_path, monkeypatch):
    """_current_play_info 跟目前要 skip 的 cur 是同一個物件（正常手動 skip）→
    帶 play_id/narration_id/elapsed_s。"""
    from music_memory import MusicMemory
    import cogs.music_cog_subsystem as subsystem

    rows = []
    monkeypatch.setattr(subsystem, "log_song_skip", lambda rec: rows.append(rec))

    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    cog = _make_cog()
    cog.bot.music_memory = mm
    cur = {"webpage_url": "https://youtu.be/dQw4w9WgXcQ", "url": "x", "requested_by": "大肚"}
    cog._current_stream_info = cur
    cog._current_play_info = cur
    cog._current_play_id = "play1"
    cog._current_narration = ("nid1", "life")
    cog._current_stream_start_time = time.time() - 5.0

    cog._record_song_skip()

    assert len(rows) == 1
    rec = rows[0]
    assert rec["play_id"] == "play1"
    assert rec["narration_id"] == "nid1"
    assert rec["mode"] == "life"
    assert rec["video_id"] == "dQw4w9WgXcQ"
    assert rec["elapsed_s"] is not None and rec["elapsed_s"] >= 5.0
    assert rec["requested_by"] == "大肚"


def test_record_song_skip_logs_none_fields_when_different_play(tmp_path, monkeypatch):
    """_current_play_info 不是目前要 skip 的這首（例如已經換了新 current）→
    play_id/narration_id/elapsed_s 全 None。"""
    from music_memory import MusicMemory
    import cogs.music_cog_subsystem as subsystem

    rows = []
    monkeypatch.setattr(subsystem, "log_song_skip", lambda rec: rows.append(rec))

    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    cog = _make_cog()
    cog.bot.music_memory = mm
    cur = {"webpage_url": "https://youtu.be/dQw4w9WgXcQ", "url": "x"}
    cog._current_stream_info = cur
    cog._current_play_info = {"webpage_url": "https://youtu.be/other", "url": "y"}
    cog._current_play_id = "play2"
    cog._current_narration = ("nid2", "quick")
    cog._current_stream_start_time = time.time() - 5.0

    cog._record_song_skip()

    assert len(rows) == 1
    rec = rows[0]
    assert rec["play_id"] is None
    assert rec["narration_id"] is None
    assert rec["mode"] is None
    assert rec["elapsed_s"] is None
