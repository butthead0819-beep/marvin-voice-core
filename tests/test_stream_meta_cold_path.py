"""TDD：冷啟動第一首歌 meta fetch — 加 ack + 5s timeout + hardcoded fallback。

2026-05-20 真實 incident（15:32:25 voice gateway 斷線）：DJ always-fire 改動
讓 _fetch_song_meta 在冷啟動跑 LLM + TTS 共 34 秒，await 卡死 event loop →
Discord disconnect。

修法：
- 抽 _meta_with_ack_fallback() helper：
  - 先 fire-and-forget _play_ack_sound 立刻填空檔（user 知道收到了）
  - asyncio.wait_for(_fetch_song_meta, 5.0) 限時
  - timeout → hardcoded fallback meta（dj.text + audio_path=None，下游 _maybe_play_dj_interjection 走即時 play_tts）

Prefetch 路徑（queue 中第 2+ 首）不受影響——已用 _prefetch_cache 預取。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj.opus")
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="唉...點周杰倫")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="k")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._COLD_META_TIMEOUT_S = 0.05
    return cog


def _info(title="周杰倫 - 雙截棍", requester="狗與露"):
    return {"title": title, "uploader": "周杰倫", "requested_by": requester, "url": "x"}


# ── 1. Fast path: meta 在 5s 內 → 正常回 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_fast_meta_returns_real_data():
    cog = _make_cog()
    expected = {"lyrics": "L", "comment": "C", "dj": {"text": "DJ 文", "audio_path": "/tmp/dj.opus"}}
    cog._fetch_song_meta = AsyncMock(return_value=expected)

    result = await cog._meta_with_ack_fallback(_info(), "狗與露")

    assert result == expected


# ── 2. Slow path: meta >5s timeout → fallback meta ──────────────────────────

@pytest.mark.asyncio
async def test_timeout_returns_fallback_meta_with_hardcoded_dj():
    cog = _make_cog()

    async def _slow_meta(info):
        await asyncio.sleep(0.5)  # >0.05s timeout
        return {"lyrics": "should not arrive", "comment": "", "dj": {"text": "x", "audio_path": "y"}}

    cog._fetch_song_meta = _slow_meta

    result = await cog._meta_with_ack_fallback(_info(title="雙截棍", requester="狗與露"), "狗與露")

    assert result["lyrics"] is None, "timeout 時 lyrics 應為 None"
    assert result["comment"] is None
    dj = result["dj"]
    assert dj is not None
    assert "雙截棍" in dj["text"], f"fallback dj text 必須含歌名: {dj['text']}"
    assert "狗與露" in dj["text"], f"fallback dj text 必須含點播者: {dj['text']}"
    assert dj["audio_path"] is None, "fallback 無預渲染 TTS（下游 _maybe_play_dj_interjection 走即時）"


# ── 4. Event loop 不被卡：fetch timeout ───────────────────────────────────

@pytest.mark.asyncio
async def test_total_wall_time_capped_at_5s_even_when_meta_hangs():
    """meta hang 也不能讓整個 call 跑太久。"""
    import time
    cog = _make_cog()

    async def _hang_forever(info):
        await asyncio.sleep(0.5)

    cog._fetch_song_meta = _hang_forever

    t0 = time.monotonic()
    result = await cog._meta_with_ack_fallback(_info(), "狗與露")
    elapsed = time.monotonic() - t0

    assert elapsed < 0.2, f"timeout 應限制 ≤0.2s，實際: {elapsed:.2f}s"
    assert result["dj"]["text"], "timeout 仍要回 fallback dj 文字"


# ── 5. Edge：requester 空 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_requester_still_returns_meta_no_crash():
    """非 user 點歌（auto_recommend 等）— 不該炸，但 fallback dj 可能無意義。"""
    cog = _make_cog()

    async def _slow(info):
        await asyncio.sleep(0.5)

    cog._fetch_song_meta = _slow

    result = await cog._meta_with_ack_fallback(_info(requester=""), "")
    assert result is not None
    assert result["dj"] is not None  # fallback always populates dj
