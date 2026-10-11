"""TDD: 播出前一刻依聊天熱度切換 DJ 口白（cogs/music_cog_tail_dj.py）。

2026-10-11 使用者定：拔掉 revival（接回熱聊原句硬接一首歌，常公式化/誤拿私事）；
熱聊時改成這輪整段不講話，直接 crossfade 進下一首；降溫後恢復一般口白。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="唉...")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.conv_buffer.get_history = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _make_vc(members):
    vc = MagicMock()
    vc.play_tts = AsyncMock(return_value=True)
    vc.play_local_file = AsyncMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    vc._tts_protected = False
    vc._intimate_mode = False
    vc.get_online_members = MagicMock(return_value=list(members))
    return vc


def _hot_entries(now, n=25):
    return [
        {"timestamp": now - 3 * (i + 1), "speaker": ("大肚" if i % 2 == 0 else "狗與露"),
         "text": f"聊天{i}"}
        for i in range(n)
    ]


# ── (a)(b) _maybe_play_dj_interjection：熱聊不講，不熱照舊播原口白 ──────────

@pytest.mark.asyncio
async def test_hot_channel_speaks_nothing_and_skips_tts():
    """熱聊 → 回 None，且 generate_audio / play_dj_on_tts_layer / play_tts 都沒被呼叫。"""
    cog = _make_cog()
    now = time.time()
    cog.bot.engine.conv_buffer.get_history = MagicMock(return_value=_hot_entries(now))
    vc = _make_vc(["大肚", "狗與露"])
    cog._vc = MagicMock(return_value=vc)

    dj = {"text": "這是原本的長口白，講了很多故事", "audio_path": "/tmp/original_dj.opus"}
    result = await cog._maybe_play_dj_interjection(dj)

    assert result is None
    cog.bot.tts_engine.generate_audio.assert_not_awaited()
    vc.play_dj_on_tts_layer.assert_not_awaited()
    vc.play_tts.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_hot_speaks_original_text_and_audio():
    """不熱 → 照舊播原本預渲染的 audio_path 與原口白文字，回 "full"。"""
    cog = _make_cog()
    vc = _make_vc(["大肚"])  # 只 1 人在線，不算熱
    cog._vc = MagicMock(return_value=vc)

    dj = {"text": "這是原本的長口白", "audio_path": "/tmp/original_dj.opus"}
    result = await cog._maybe_play_dj_interjection(dj)

    assert result == "full"
    cog.bot.tts_engine.generate_audio.assert_not_awaited()
    vc.play_dj_on_tts_layer.assert_awaited_once_with("/tmp/original_dj.opus", text=dj["text"])


# ── (c) 尾段點火：熱聊 → crossfade 不講話；不熱且非懷舊 → 照舊走口白 ────────

def _cur_info(duration=180.0):
    return {"title": "周杰倫 - 夜曲", "url": "https://ex/cur", "duration": duration,
            "requested_by": "大肚"}


def _next_info():
    return {"title": "陶喆 - 普通朋友", "url": "https://ex/next", "requested_by": "狗與露"}


def _prime(cog, cur):
    cog._current_stream_info = cur
    cog._current_song_skipped = False
    cog.stream_mode = True
    cog._maybe_play_dj_interjection = AsyncMock()
    cog._start_music_preload = MagicMock()


@pytest.mark.asyncio
async def test_tail_fire_hot_crossfades_without_resolving_dj_meta():
    """熱聊時點火 → 走 _crossfade_into_silent，不呼叫 _resolve_tail_dj_meta。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    now = time.time()
    cog.bot.engine.conv_buffer.get_history = MagicMock(return_value=_hot_entries(now))
    vc = _make_vc(["大肚", "狗與露"])
    cog._vc = MagicMock(return_value=vc)
    _prime(cog, cur)
    cog._resolve_tail_dj_meta = AsyncMock()
    cog._crossfade_into_silent = AsyncMock()

    await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._crossfade_into_silent.assert_awaited_once()
    cog._resolve_tail_dj_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_tail_fire_not_hot_non_nostalgia_resolves_dj_meta():
    """不熱且下一首非懷舊位 → 不走 crossfade，照舊呼叫 _resolve_tail_dj_meta。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    vc = _make_vc(["大肚"])  # 只 1 人在線，不算熱
    cog._vc = MagicMock(return_value=vc)
    _prime(cog, cur)
    cog._resolve_tail_dj_meta = AsyncMock(return_value=None)
    cog._crossfade_into_silent = AsyncMock()

    await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._crossfade_into_silent.assert_not_awaited()
    cog._resolve_tail_dj_meta.assert_awaited_once()


# ── (d) revival 已拔除；_fetch_dj_interjection_raw 不再回 short_text ────────

def test_revival_not_in_modes():
    from dj_narration_orchestrator import MODES
    assert "revival" not in MODES


@pytest.mark.asyncio
async def test_fetch_dj_interjection_raw_has_no_short_text_key():
    cog = _make_cog()
    dj = await cog._fetch_dj_interjection_raw(
        {"title": "周杰倫 - 夜曲", "requested_by": "大肚", "url": "https://ex/x"}
    )
    assert dj is not None
    assert "short_text" not in dj
