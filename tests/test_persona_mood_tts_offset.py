"""人格週排程 mood（bot.router.dna['persona_tag']）疊加 TTS rate/pitch offset。

驗證：persona_behavior_map.yaml 的 rate_offset_percent / pitch_offset_hz 能透過
PlaybackMixin._apply_persona_mood_tts_offset 反映到最終 edge-tts 參數，
不只影響 prompt 語氣（marvin_prompts.get_persona_modifiers 既有用途）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from cogs.voice_controller_playback import _shift_hz_string, _shift_percent_string


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    return cog


def test_shift_percent_string_adds_offset_and_formats():
    assert _shift_percent_string("-20%", 15) == "-5%"
    assert _shift_percent_string("-20%", 0) == "-20%"
    assert _shift_percent_string(None, 10) == "+10%"


def test_shift_percent_string_clamps():
    assert _shift_percent_string("-50%", -30) == "-60%"


def test_shift_hz_string_adds_offset_and_formats():
    assert _shift_hz_string("-15Hz", 5) == "-10Hz"
    assert _shift_hz_string(None, -4) == "-4Hz"


def test_apply_persona_mood_offset_no_router_returns_unchanged():
    cog = _make_cog()
    cog.bot = MagicMock(spec=[])  # no .router attribute at all
    tp = {"rate": "-20%", "pitch": "-15Hz"}
    assert cog._apply_persona_mood_tts_offset(tp) == tp


def test_apply_persona_mood_offset_manic_persona_speeds_up_and_raises_pitch():
    cog = _make_cog()
    cog.bot.router = MagicMock()
    cog.bot.router.dna = {"persona_tag": "躁鬱機器"}
    tp = {"rate": "-20%", "pitch": "-15Hz"}
    out = cog._apply_persona_mood_tts_offset(tp)
    assert out["rate"] == "-5%"   # -20 + 15
    assert out["pitch"] == "-10Hz"  # -15 + 5
    assert tp == {"rate": "-20%", "pitch": "-15Hz"}  # 不修改原 dict


def test_apply_persona_mood_offset_default_persona_is_baseline_zero():
    cog = _make_cog()
    cog.bot.router = MagicMock()
    cog.bot.router.dna = {"persona_tag": "厭世機器人馬文"}
    tp = {"rate": "-20%", "pitch": "-15Hz"}
    assert cog._apply_persona_mood_tts_offset(tp) == tp


def test_apply_persona_mood_offset_shutdown_persona_slows_and_lowers():
    cog = _make_cog()
    cog.bot.router = MagicMock()
    cog.bot.router.dna = {"persona_tag": "邏輯關機"}
    tp = {"rate": "-20%", "pitch": "-15Hz"}
    out = cog._apply_persona_mood_tts_offset(tp)
    assert out["rate"] == "-45%"    # -20 + -25
    assert out["pitch"] == "-27Hz"  # -15 + -12


def test_apply_persona_mood_offset_unknown_tag_falls_back_to_default_persona():
    cog = _make_cog()
    cog.bot.router = MagicMock()
    cog.bot.router.dna = {"persona_tag": "不存在的標籤"}
    tp = {"rate": "-20%", "pitch": "-15Hz"}
    # get_persona_modifiers() 對未知 tag 優雅降級成「厭世機器人馬文」（offset=0）
    assert cog._apply_persona_mood_tts_offset(tp) == tp
