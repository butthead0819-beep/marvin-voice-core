"""
VoiceController._build_speak_context 要把 voice 狀態翻成 ctx.mode 字串：
  game_mode  → "game"   (precedence 最高)
  stream_mode → "stream"
  radio_mode → "radio"
  都沒 → "normal"

多重 flag 同時為真時取最受限：game > stream > radio > normal。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_cog():
    """繞過 VoiceController 全建構，直接抓 _build_speak_context 綁到 stub。"""
    from cogs.voice_controller import VoiceController

    cog = VoiceController.__new__(VoiceController)
    # _build_speak_context 摸的欄位
    cog._last_room_stt_time = 0.0
    cog.active_text_channel = MagicMock()
    cog.active_text_channel.id = 100
    cog.active_text_channel.guild = MagicMock()
    cog.active_text_channel.guild.id = 1
    cog._room_mood_store = MagicMock()
    cog._room_mood_store.get.return_value = None
    cog.get_online_members = lambda: ["Alice"]
    # stream_mode/radio_mode 是 proxy property，需要 bot
    cog.bot = MagicMock()
    cog.bot.cogs.get.return_value = None
    cog._stream_mode_local = False
    cog._radio_mode_local = False
    # 3 個 mode flag 預設 False
    cog.game_mode = False
    cog.stream_mode = False
    cog.radio_mode = False
    return cog


def test_mode_normal_when_all_flags_off():
    cog = _make_cog()
    ctx = cog._build_speak_context(trigger="idle_tick")
    assert ctx.mode == "normal"


def test_mode_game_when_game_mode():
    cog = _make_cog()
    cog.game_mode = True
    ctx = cog._build_speak_context(trigger="idle_tick")
    assert ctx.mode == "game"


def test_mode_stream_when_stream_mode():
    cog = _make_cog()
    cog.stream_mode = True
    ctx = cog._build_speak_context(trigger="idle_tick")
    assert ctx.mode == "stream"


def test_mode_radio_when_radio_mode():
    cog = _make_cog()
    cog.radio_mode = True
    ctx = cog._build_speak_context(trigger="idle_tick")
    assert ctx.mode == "radio"


# ── precedence: game > stream > radio > normal ──────────────────────────────


def test_mode_game_beats_stream_when_both_on():
    cog = _make_cog()
    cog.game_mode = True
    cog.stream_mode = True
    ctx = cog._build_speak_context(trigger="idle_tick")
    assert ctx.mode == "game"


def test_mode_stream_beats_radio_when_both_on():
    cog = _make_cog()
    cog.stream_mode = True
    cog.radio_mode = True
    ctx = cog._build_speak_context(trigger="idle_tick")
    assert ctx.mode == "stream"


def test_mode_game_beats_all_when_all_on():
    cog = _make_cog()
    cog.game_mode = True
    cog.stream_mode = True
    cog.radio_mode = True
    ctx = cog._build_speak_context(trigger="idle_tick")
    assert ctx.mode == "game"
