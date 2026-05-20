"""TDD: DJ interjection 必須每首 user-requested 歌都唸出歌名 + 點播者。

2026-05-20 user 抱怨「點完歌無聲無息」。挖出根因：
- voice_controller.py:5692-5696 的 should_play gate 只有 25% 隨機觸發
- 第一次點的歌 + 無 prior reaction → 75% 機率完全沒 DJ → user 不知道是否點到

修法：
1. 砍隨機 gate — user-requested 永遠播
2. LLM 失敗 / text 太短 → hardcoded fallback「下一首是《X》，Y 點的」
3. Prompt 強化要求點名歌名（marvin_prompts.py 的 dj_interjection）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(
        return_value="唉...大肚又懷舊了，這首夜曲，2005 年的眼淚"
    )
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None

    # MusicMemory mock — 沒有 prior play/feelings/lyric_match
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.stt_logger = MagicMock()
    return cog


def _info(title="周杰倫 - 夜曲", requester="大肚"):
    return {
        "title": title,
        "uploader": "周杰倫",
        "requested_by": requester,
        "url": "https://example/x",
    }


# ── 1. Always-announce 核心 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dj_fires_for_first_time_song_no_prior_reaction():
    """第一次點的歌 + 0 prior signal → 必須觸發 DJ（修前 75% 沉默）。"""
    cog = _make_cog()
    # 強制 random 回傳 0.99，舊邏輯會被 gate 擋掉
    with patch("cogs.voice_controller.random.random", return_value=0.99):
        result = await cog._fetch_dj_interjection_raw(_info())
    assert result is not None, "first-time user-requested song MUST get DJ announcement"
    assert isinstance(result, dict)
    assert result.get("text"), "DJ result must include non-empty text"


@pytest.mark.asyncio
async def test_dj_fires_even_when_random_always_high():
    """跑 5 次都用最壞 random 值，仍必須每次都有 DJ。"""
    cog = _make_cog()
    for _ in range(5):
        with patch("cogs.voice_controller.random.random", return_value=0.99):
            result = await cog._fetch_dj_interjection_raw(_info())
        assert result is not None


# ── 2. Skip 條件不變（保護 Marvin 自薦 / 無 requester）────────────────────

@pytest.mark.asyncio
async def test_dj_skipped_for_marvin_recommended():
    cog = _make_cog()
    result = await cog._fetch_dj_interjection_raw(_info(requester="Marvin Recommended"))
    assert result is None, "Marvin-picked songs intentionally skip DJ"


@pytest.mark.asyncio
async def test_dj_skipped_for_empty_requester():
    cog = _make_cog()
    result = await cog._fetch_dj_interjection_raw(_info(requester=""))
    assert result is None


# ── 3. Fallback 保證一定有聲音 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dj_fallback_when_llm_raises():
    """LLM 炸 → hardcoded fallback 仍含 title + requester。"""
    cog = _make_cog()
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(
        side_effect=Exception("LLM connection lost")
    )
    result = await cog._fetch_dj_interjection_raw(_info(title="夜曲", requester="大肚"))
    assert result is not None, "fallback must guarantee announcement even when LLM fails"
    text = result["text"]
    assert "夜曲" in text, f"fallback text must mention song title; got: {text}"
    assert "大肚" in text, f"fallback text must mention requester; got: {text}"


@pytest.mark.asyncio
async def test_dj_fallback_when_llm_returns_empty():
    """LLM 回空字串 → hardcoded fallback。"""
    cog = _make_cog()
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="")
    result = await cog._fetch_dj_interjection_raw(_info(title="稻香", requester="狗與露"))
    assert result is not None
    assert "稻香" in result["text"]
    assert "狗與露" in result["text"]


@pytest.mark.asyncio
async def test_dj_fallback_when_llm_returns_too_short():
    """LLM 回 1 字元 → hardcoded fallback。"""
    cog = _make_cog()
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="嗯")
    result = await cog._fetch_dj_interjection_raw(_info(title="七里香", requester="weakgogo"))
    assert result is not None
    assert "七里香" in result["text"]
    assert "weakgogo" in result["text"]


# ── 4. Happy path + TTS 整合 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dj_happy_path_returns_text_and_audio():
    cog = _make_cog()
    result = await cog._fetch_dj_interjection_raw(_info())
    assert result is not None
    assert result["text"]
    assert result["audio_path"] == "/tmp/dj_audio.opus"


@pytest.mark.asyncio
async def test_dj_returns_text_only_when_tts_fails():
    """LLM 成功但 TTS 預渲染失敗 → text 仍存在，audio_path=None；
    上游 _maybe_play_dj_interjection 會走即時 play_tts 路徑（沒有沉默）。"""
    cog = _make_cog()
    cog.bot.tts_engine.generate_audio = AsyncMock(side_effect=Exception("TTS engine down"))
    result = await cog._fetch_dj_interjection_raw(_info())
    assert result is not None
    assert result["text"]
    assert result["audio_path"] is None


@pytest.mark.asyncio
async def test_dj_fallback_audio_render_attempted():
    """Fallback 文字也應該嘗試 TTS 預渲染（讓上游能跟 intro 混音）。"""
    cog = _make_cog()
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="")
    await cog._fetch_dj_interjection_raw(_info())
    # generate_audio 至少被呼叫一次（針對 fallback 文字）
    assert cog.bot.tts_engine.generate_audio.await_count >= 1
