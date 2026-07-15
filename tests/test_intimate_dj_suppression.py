"""TDD: 私語模式（_intimate_mode=True）應壓制 DJ 播報，不論 autopilot 還是今夜歌單路徑。

路徑：cogs/music_cog.py::_maybe_play_dj_interjection
守衛插入點：在 `if vc is None: return` 之後加一行早退。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog():
    """最小 MusicCog stub，_vc() 預設回 None。"""
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value=None)
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="唉...")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _make_vc(intimate: bool) -> MagicMock:
    """構造 VoiceController mock，_intimate_mode 明確設置。"""
    vc = MagicMock()
    vc.play_tts = AsyncMock()
    vc.play_local_file = AsyncMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    vc._tts_protected = False
    vc._intimate_mode = intimate
    return vc


def _make_vc_no_intimate() -> SimpleNamespace:
    """構造無 _intimate_mode 屬性的 stub（getattr 依賴 default False）。"""
    vc = SimpleNamespace()
    vc.play_tts = AsyncMock()
    vc.play_local_file = AsyncMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    vc._tts_protected = False
    return vc


# ── 1. 私語模式 ON：完全壓制 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_intimate_on_suppresses_dj_blurb():
    """_intimate_mode=True, 純文字 dj → play_tts/play_local_file 皆不執行。"""
    cog = _make_cog()
    vc = _make_vc(intimate=True)
    cog._vc = MagicMock(return_value=vc)

    await cog._maybe_play_dj_interjection({'text': '下一首是《夜曲》，大肚點的。', 'audio_path': None})

    vc.play_tts.assert_not_awaited()
    vc.play_local_file.assert_not_awaited()
    assert vc._tts_protected is False, "私語模式不應設置 _tts_protected"


@pytest.mark.asyncio
async def test_intimate_on_suppresses_even_with_audio_path(tmp_path):
    """_intimate_mode=True, audio_path 存在 → play_local_file 不執行。"""
    cog = _make_cog()
    vc = _make_vc(intimate=True)
    cog._vc = MagicMock(return_value=vc)

    audio_file = tmp_path / "dj.opus"
    audio_file.write_bytes(b"fake_audio")

    await cog._maybe_play_dj_interjection({'text': '播報文字', 'audio_path': str(audio_file)})

    vc.play_dj_on_tts_layer.assert_not_awaited()
    vc.play_local_file.assert_not_awaited()
    vc.play_tts.assert_not_awaited()


# ── 2. 私語模式 OFF / 未設：正常播放 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_intimate_off_text_only_speaks():
    """_intimate_mode=False, 純文字 dj → play_tts 執行。"""
    cog = _make_cog()
    vc = _make_vc(intimate=False)
    cog._vc = MagicMock(return_value=vc)

    await cog._maybe_play_dj_interjection({'text': '下一首是《七里香》', 'audio_path': None})

    vc.play_tts.assert_awaited_once()
    vc.play_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_intimate_absent_speaks():
    """vc 沒有 _intimate_mode 屬性 → getattr 預設 False → play_tts 執行。"""
    cog = _make_cog()
    vc = _make_vc_no_intimate()
    cog._vc = MagicMock(return_value=vc)

    await cog._maybe_play_dj_interjection({'text': '下一首是《稻香》', 'audio_path': None})

    vc.play_tts.assert_awaited_once()


@pytest.mark.asyncio
async def test_intimate_off_with_audio_plays_on_tts_layer(tmp_path):
    """_intimate_mode=False, audio_path 存在 → play_dj_on_tts_layer 執行（TTS 層），
    play_local_file（音樂層）/play_tts 皆不執行。"""
    cog = _make_cog()
    vc = _make_vc(intimate=False)
    cog._vc = MagicMock(return_value=vc)

    audio_file = tmp_path / "dj.opus"
    audio_file.write_bytes(b"fake_audio")

    await cog._maybe_play_dj_interjection({'text': '播報', 'audio_path': str(audio_file)})

    vc.play_dj_on_tts_layer.assert_awaited_once()
    vc.play_local_file.assert_not_awaited()
    vc.play_tts.assert_not_awaited()


# ── 3. 既有早退條件不受影響 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_text_returns_early():
    """dj text 為空 → play_tts/play_local_file 皆不執行。"""
    cog = _make_cog()
    vc = _make_vc(intimate=False)
    cog._vc = MagicMock(return_value=vc)

    await cog._maybe_play_dj_interjection({'text': '', 'audio_path': None})

    vc.play_tts.assert_not_awaited()
    vc.play_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_vc_none_returns_early():
    """_vc() 回 None → 不拋例外。"""
    cog = _make_cog()
    cog._vc = MagicMock(return_value=None)

    # 安靜返回，不拋例外即成功
    await cog._maybe_play_dj_interjection({'text': '有文字', 'audio_path': None})
