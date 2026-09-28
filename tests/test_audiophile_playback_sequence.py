"""TDD：導聆 Sequential Pre-roll（docs/PLAN_audiophile_music_tour.md Phase 3）。

硬性規則：info['_audiophile_guide'] 為真時——
  1. 導聆音檔先上 TTS 層（play_dj_on_tts_layer，包在 _protected_tts_window 內）
  2. 等滿「導聆秒數 + 1 秒留白」才輪到 play_stream_song（絕不偷跑前奏）
  3. 歌從 00:00 開播：highlight_start_s 清成 None（不走熱力圖精華起播）
  4. 本首開頭 DJ 口白讓位（導聆就是這首的開場）
  5. 上一首的尾段 DJ 不幫導聆歌講話、不用精華起點預解碼它
  6. 導聆中 stop / skip → 清掉 TTS 層、不再乾等（skip＝跳過導聆，歌照樣從 0 播）

等待用「累加 sleep 請求秒數」判斷，不比牆鐘（flaky_test_pump_double_timing 教訓）。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_real_sleep = asyncio.sleep


def _make_cog_with_vc(events):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    vc = MagicMock()
    vc.active_text_channel = None
    vc.voice_client = None
    vc.get_online_members = MagicMock(return_value=[])
    vc.last_marvin_speech_time = 0
    vc.stt_logger = MagicMock()

    @contextlib.contextmanager
    def _protected():
        events.append(("protect_enter",))
        try:
            yield
        finally:
            events.append(("protect_exit",))

    vc._protected_tts_window = _protected

    async def _play_guide(path, **kw):
        events.append(("guide", path, kw.get("text")))
        return True

    vc.play_dj_on_tts_layer = AsyncMock(side_effect=_play_guide)
    vc._mixer.clear_tts = MagicMock(side_effect=lambda: events.append(("clear_tts",)))
    bot.cogs.get.return_value = vc

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._auto_recommend = AsyncMock()
    cog._last_resort_replay = AsyncMock(return_value=False)
    cog._maybe_play_dj_interjection = AsyncMock()
    cog._splice_owner_voice_clip = AsyncMock(side_effect=lambda path, info: path)
    cog._start_music_preload = MagicMock(
        side_effect=lambda info: events.append(("preload", info.get("highlight_start_s"))))

    async def _play(url, title, **kw):
        events.append(("play", kw.get("highlight_start_s"), kw.get("dj_audio_path")))

    cog.play_stream_song = AsyncMock(side_effect=_play)
    return cog, vc


def _done_future(value):
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


def _song(tmp_path, *, guide=True, dur=20.0, audio=True):
    info = {"title": "周杰倫 - 雙截棍", "url": "https://ex/cdn", "webpage_url": "https://youtube.com/watch?v=x",
            "requested_by": "狗與露", "highlight_start_s": 42.0}
    if guide:
        path = tmp_path / "guide.mp3"
        path.write_bytes(b"x")
        info.update({"_audiophile_guide": True,
                     "_audiophile_guide_audio": str(path) if audio else None,
                     "_audiophile_guide_dur": dur,
                     "_audiophile_guide_text": "導聆台詞"})
    return info


def _fake_sleep(events, on_sleep=None):
    async def _sleep(d, *a, **kw):
        events.append(("sleep", d))
        if on_sleep is not None:
            on_sleep()
        await _real_sleep(0)
    return _sleep


async def _run_loop(cog, song, events, *, meta=None, on_sleep=None):
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(meta)
    with patch("cogs.music_cog._get_puck_client", return_value=None), \
         patch("bridge_emitters.emit_music_ended_to_bridge", new=AsyncMock()), \
         patch("asyncio.sleep", new=_fake_sleep(events, on_sleep)):
        await cog._stream_loop()
        await _real_sleep(0)


def _kinds(events):
    return [e[0] for e in events]


def _slept_between(events, start_kind, end_kind):
    kinds = _kinds(events)
    i, j = kinds.index(start_kind), kinds.index(end_kind)
    return sum(e[1] for e in events[i:j] if e[0] == "sleep")


@pytest.mark.asyncio
async def test_guide_plays_fully_before_song_and_song_starts_from_zero(tmp_path):
    events = []
    cog, vc = _make_cog_with_vc(events)
    song = _song(tmp_path, dur=20.0)

    await _run_loop(cog, song, events)

    kinds = _kinds(events)
    assert "guide" in kinds and "play" in kinds
    # 1. 導聆在 protected window 內先播
    assert kinds.index("protect_enter") < kinds.index("guide") < kinds.index("protect_exit")
    guide_ev = events[kinds.index("guide")]
    assert guide_ev[1] == song["_audiophile_guide_audio"]
    assert guide_ev[2] == "導聆台詞"
    # 2. 等滿導聆秒數 + 1 秒留白，且整段等待都在 protected window 內
    assert _slept_between(events, "guide", "protect_exit") >= 21.0
    assert kinds.index("protect_exit") < kinds.index("play")
    # 3. 從 00:00 開播
    play_ev = events[kinds.index("play")]
    assert play_ev[1] is None
    # 4. 導聆期間背景預解碼，而且是從 0 秒預解碼
    assert ("preload", None) in events
    assert kinds.index("preload") < kinds.index("play")
    # 導聆沒被中斷 → 不清 TTS 層
    assert "clear_tts" not in kinds


@pytest.mark.asyncio
async def test_no_guide_flag_keeps_existing_behavior(tmp_path):
    events = []
    cog, vc = _make_cog_with_vc(events)
    song = _song(tmp_path, guide=False)

    await _run_loop(cog, song, events)

    vc.play_dj_on_tts_layer.assert_not_awaited()
    play_ev = events[_kinds(events).index("play")]
    assert play_ev[1] == 42.0  # 精華起播照舊
    assert "preload" not in _kinds(events)


@pytest.mark.asyncio
async def test_guide_flag_without_rendered_audio_skips_preroll_but_still_from_zero(tmp_path):
    """導聆渲染失敗（沒音檔 / 秒數 0）→ 不播 pre-roll、不乾等，但導聆歌照樣從 0 播。"""
    events = []
    cog, vc = _make_cog_with_vc(events)
    song = _song(tmp_path, dur=0.0)

    await _run_loop(cog, song, events)

    vc.play_dj_on_tts_layer.assert_not_awaited()
    play_ev = events[_kinds(events).index("play")]
    assert play_ev[1] is None
    assert _slept_between(events, "protect_enter", "play") == 0 if "protect_enter" in _kinds(events) else True
    assert sum(e[1] for e in events[:_kinds(events).index("play")] if e[0] == "sleep") < 1.0


@pytest.mark.asyncio
async def test_opening_dj_yields_to_guide(tmp_path):
    """meta 已就緒且有預渲染開頭 DJ → 導聆歌不混開頭 DJ、不另外插話。"""
    events = []
    cog, vc = _make_cog_with_vc(events)
    song = _song(tmp_path)
    dj_path = tmp_path / "dj.opus"
    dj_path.write_bytes(b"x")

    await _run_loop(cog, song, events,
                    meta={"dj": {"audio_path": str(dj_path)}, "comment": None, "lyrics": None})

    play_ev = events[_kinds(events).index("play")]
    assert play_ev[2] is None  # dj_audio_path
    cog._maybe_play_dj_interjection.assert_not_awaited()
    cog._splice_owner_voice_clip.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_during_guide_cuts_guide_then_plays_song_from_zero(tmp_path):
    events = []
    cog, vc = _make_cog_with_vc(events)
    song = _song(tmp_path, dur=20.0)

    def _on_sleep():
        if "guide" in _kinds(events):
            cog._current_song_skipped = True

    await _run_loop(cog, song, events, on_sleep=_on_sleep)

    kinds = _kinds(events)
    assert "clear_tts" in kinds
    assert kinds.index("clear_tts") < kinds.index("play")
    assert _slept_between(events, "guide", "play") < 21.0
    assert events[kinds.index("play")][1] is None


@pytest.mark.asyncio
async def test_stop_during_guide_cuts_guide(tmp_path):
    events = []
    cog, vc = _make_cog_with_vc(events)
    song = _song(tmp_path, dur=20.0)

    def _on_sleep():
        if "guide" in _kinds(events):
            cog.stream_mode = False

    await _run_loop(cog, song, events, on_sleep=_on_sleep)

    kinds = _kinds(events)
    assert "clear_tts" in kinds
    end = kinds.index("play") if "play" in kinds else len(events)
    assert sum(e[1] for e in events[kinds.index("guide"):end] if e[0] == "sleep") < 21.0


# ── 上一首的尾段 DJ 不碰導聆歌 ───────────────────────────────────────────────

def _make_tail_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


@pytest.mark.asyncio
async def test_tail_dj_leaves_guided_next_song_alone():
    cog = _make_tail_cog()
    cur = {"title": "周杰倫 - 夜曲", "url": "https://ex/cur", "duration": 180.0, "requested_by": "大肚"}
    nxt = {"title": "周杰倫 - 雙截棍", "url": "https://ex/next", "requested_by": "狗與露",
           "highlight_start_s": 42.0, "_audiophile_guide": True}
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": {"text": "…", "audio_path": "/tmp/dj.opus"}})
    cog._current_stream_info = cur
    cog._current_song_skipped = False
    cog.stream_mode = True
    cog._maybe_play_dj_interjection = AsyncMock()
    cog._start_music_preload = MagicMock()

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_awaited()
    cog._start_music_preload.assert_not_called()
    assert not nxt.get("_dj_played_in_tail")
