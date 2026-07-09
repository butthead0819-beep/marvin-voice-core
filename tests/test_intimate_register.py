"""TDD — T4: MARVIN_INTIMATE_MODE flag + 輕聲 TTS prosody register。

先紅後綠：
  - _INTIMATE_TTS_PARAMS / _resolve_tts_params 不存在 → AttributeError → RED
  - MARVIN_INTIMATE_MODE flag 未寫進 connection.py → _intimate_mode 未被設 → RED
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.voice_controller_connection import ConnectionMixin


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_cog():
    """Minimal VoiceController（對齊 test_playback_tts_path._make_cog 骨架）。"""
    bot = MagicMock()
    bot.guilds = []
    bot.cogs.get.return_value = None

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


def _make_fake_self():
    """造 ConnectionMixin mock self（對齊 test_local_input_seam._make_fake_self）。"""
    fake = MagicMock()
    fake.bot.engine.process_audio_slice = AsyncMock()
    fake.bot.engine.start = MagicMock()
    fake.bot.loop = MagicMock()
    fake.set_local_speaker.side_effect = lambda device: setattr(fake, "_local_speaker", device)
    return fake


# ── _INTIMATE_TTS_MAP 常數確認（T6 對比舒緩 bucket）────────────────────────────

def test_intimate_tts_map_has_three_buckets_calm_is_gentle_baseline():
    """_INTIMATE_TTS_MAP 存在（代替 T4 flat _INTIMATE_TTS_PARAMS）；
    CALM baseline == 舊 T4 rate/pitch + volume 欄位；三 bucket 各異。"""
    cog = _make_cog()
    assert hasattr(cog, "_INTIMATE_TTS_MAP"), "_INTIMATE_TTS_MAP 不存在"
    calm = cog._INTIMATE_TTS_MAP.get("neutral")
    agitated = cog._INTIMATE_TTS_MAP.get("excited")
    low = cog._INTIMATE_TTS_MAP.get("sad")
    assert calm is not None
    assert agitated is not None
    assert low is not None
    # CALM 基準 == T4 flat 值 + volume 欄位
    assert calm == {"rate": "-28%", "pitch": "-22Hz", "volume": "-18%"}
    # 三 bucket 各異（對比舒緩有意義）
    assert agitated != calm
    assert low != calm
    assert agitated != low
    # 每個 bucket 都帶 volume 欄位
    assert "volume" in agitated
    assert "volume" in low


# ── _resolve_tts_params OFF（byte-equivalence）────────────────────────────────

@pytest.mark.parametrize("tag", ["neutral", "excited", "sad", "nemo", "marmo", "robotic"])
def test_resolve_tts_params_off_explicit_false_byte_equiv(tag):
    """_intimate_mode=False → 各 tag 與 _EMOTION_TTS_PARAMS inline lookup 完全一致。"""
    cog = _make_cog()
    cog._intimate_mode = False
    expected = cog._EMOTION_TTS_PARAMS.get(tag, cog._EMOTION_TTS_PARAMS["neutral"])
    assert cog._resolve_tts_params(tag) == expected


@pytest.mark.parametrize("tag", ["neutral", "excited", "sad", "nemo", "marmo", "robotic"])
def test_resolve_tts_params_off_absent_byte_equiv(tag):
    """_intimate_mode 不存在（Discord 路徑，getattr default False）→ 與 inline lookup 一致。"""
    cog = _make_cog()
    # Discord 路徑從不設 _intimate_mode；確保屬性不存在（防禦性清除）
    try:
        del cog._intimate_mode  # type: ignore[misc]
    except AttributeError:
        pass
    expected = cog._EMOTION_TTS_PARAMS.get(tag, cog._EMOTION_TTS_PARAMS["neutral"])
    assert cog._resolve_tts_params(tag) == expected


def test_resolve_tts_params_off_unknown_tag_falls_back_to_neutral():
    """不認識的 tag → neutral（inline lookup fallback，intimate OFF）。"""
    cog = _make_cog()
    cog._intimate_mode = False
    assert cog._resolve_tts_params("__unknown__") == cog._EMOTION_TTS_PARAMS["neutral"]


# ── _resolve_tts_params ON（intimate override）────────────────────────────────

@pytest.mark.parametrize("tag,expected_rate,expected_volume", [
    ("excited",    "-30%", "-20%"),   # AGITATED bucket
    ("angry",      "-30%", "-20%"),   # AGITATED bucket
    ("sad",        "-22%", "-12%"),   # LOW bucket
    ("depressed",  "-22%", "-12%"),   # LOW bucket
    ("neutral",    "-28%", "-18%"),   # CALM bucket
    ("__unknown__","-28%", "-18%"),   # 未知 tag → CALM default
])
def test_resolve_tts_params_on_bucketed_contrast_soothing(tag, expected_rate, expected_volume):
    """_intimate_mode=True → emotion_tag 路由到對應 bucket（含 volume）。"""
    cog = _make_cog()
    cog._intimate_mode = True
    result = cog._resolve_tts_params(tag)
    assert result["rate"] == expected_rate, f"tag={tag} rate 錯誤"
    assert "volume" in result, f"tag={tag} 缺少 volume 欄位"
    assert result["volume"] == expected_volume, f"tag={tag} volume 錯誤"


def test_resolve_tts_params_on_agitated_distinct_from_calm_and_low():
    """親密模式：AGITATED ≠ CALM ≠ LOW（三 bucket 真的不同）。"""
    cog = _make_cog()
    cog._intimate_mode = True
    agitated = cog._resolve_tts_params("excited")
    low = cog._resolve_tts_params("sad")
    calm = cog._resolve_tts_params("neutral")
    assert agitated != calm
    assert low != calm
    assert agitated != low


# ── flag wiring: start_local_listening ────────────────────────────────────────

def test_start_local_listening_sets_intimate_mode_true_when_env_1(monkeypatch):
    """MARVIN_INTIMATE_MODE=1 → start_local_listening 設 self._intimate_mode is True。"""
    monkeypatch.setenv("MARVIN_INTIMATE_MODE", "1")
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert fake._intimate_mode is True


def test_start_local_listening_sets_intimate_mode_true_when_env_true(monkeypatch):
    """MARVIN_INTIMATE_MODE=true → self._intimate_mode is True。"""
    monkeypatch.setenv("MARVIN_INTIMATE_MODE", "true")
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert fake._intimate_mode is True


def test_start_local_listening_sets_intimate_mode_false_when_env_unset(monkeypatch):
    """MARVIN_INTIMATE_MODE 未設 → self._intimate_mode is False。"""
    monkeypatch.delenv("MARVIN_INTIMATE_MODE", raising=False)
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert fake._intimate_mode is False


def test_start_local_listening_sets_intimate_mode_false_when_env_0(monkeypatch):
    """MARVIN_INTIMATE_MODE=0 → self._intimate_mode is False。"""
    monkeypatch.setenv("MARVIN_INTIMATE_MODE", "0")
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert fake._intimate_mode is False


def test_start_local_listening_sets_intimate_mode_false_when_env_false(monkeypatch):
    """MARVIN_INTIMATE_MODE=false → self._intimate_mode is False。"""
    monkeypatch.setenv("MARVIN_INTIMATE_MODE", "false")
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert fake._intimate_mode is False


# ── flag wiring: start_satellite_listening ────────────────────────────────────

def test_start_satellite_listening_sets_intimate_mode_true_when_env_1(monkeypatch):
    """MARVIN_INTIMATE_MODE=1 → start_satellite_listening 設 self._intimate_mode is True。"""
    monkeypatch.setenv("MARVIN_INTIMATE_MODE", "1")
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert fake._intimate_mode is True


def test_start_satellite_listening_sets_intimate_mode_false_when_env_unset(monkeypatch):
    """MARVIN_INTIMATE_MODE 未設 → self._intimate_mode is False。"""
    monkeypatch.delenv("MARVIN_INTIMATE_MODE", raising=False)
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert fake._intimate_mode is False
