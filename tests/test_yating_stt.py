"""雅婷台語雲端 STT lane — client 純函式 + 引擎路由/降級。

只有 NAN_SPEAKER_IDS 內的 user（陳進文）走雅婷；其餘一律不碰。
任何失敗（缺金鑰/缺音訊/網路/逾時）都回 ("",{}) 讓引擎降級回 Swift。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

import yating_stt


# ── client 純函式 ────────────────────────────────────────────────────────────

def test_pcm16_from_float_basic():
    audio = np.array([0.0, 1.0, -1.0, 0.5], dtype=np.float32)
    pcm = yating_stt.pcm16_from_float(audio)
    got = np.frombuffer(pcm, dtype="<i2")
    assert list(got) == [0, 32767, -32767, 16383]


def test_pcm16_from_float_clips_out_of_range():
    audio = np.array([2.0, -2.0], dtype=np.float32)  # 超出 [-1,1]
    got = np.frombuffer(yating_stt.pcm16_from_float(audio), dtype="<i2")
    assert list(got) == [32767, -32767]


def test_pcm16_from_float_empty():
    assert yating_stt.pcm16_from_float(np.array([], dtype=np.float32)) == b""
    assert yating_stt.pcm16_from_float(None) == b""


@pytest.mark.asyncio
async def test_transcribe_empty_without_key_or_pcm():
    assert await yating_stt.transcribe("", b"x") == ""
    assert await yating_stt.transcribe("key", b"") == ""


# ── 引擎路由 / 降級 ──────────────────────────────────────────────────────────

def _engine():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        return DiscordVoiceEngine(bot)


def test_is_nan_speaker_allowlist(monkeypatch):
    eng = _engine()
    monkeypatch.setenv("NAN_SPEAKER_IDS", "1352663048132886531, 999")
    assert eng._is_nan_speaker(1352663048132886531) is True
    assert eng._is_nan_speaker(999) is True
    assert eng._is_nan_speaker(123) is False
    assert eng._is_nan_speaker(None) is False


def test_is_nan_speaker_empty_env_routes_nobody(monkeypatch):
    eng = _engine()
    monkeypatch.delenv("NAN_SPEAKER_IDS", raising=False)
    assert eng._is_nan_speaker(1352663048132886531) is False  # 預設沒人走雅婷


@pytest.mark.asyncio
async def test_run_yating_degrades_without_key(monkeypatch):
    eng = _engine()
    monkeypatch.delenv("YATING_API_KEY", raising=False)
    audio = np.zeros(1600, dtype=np.float32)
    assert await eng._run_yating_stt(audio) == ("", {})


@pytest.mark.asyncio
async def test_run_yating_degrades_without_audio(monkeypatch):
    eng = _engine()
    monkeypatch.setenv("YATING_API_KEY", "k")
    assert await eng._run_yating_stt(None) == ("", {})


@pytest.mark.asyncio
async def test_run_yating_success(monkeypatch):
    eng = _engine()
    monkeypatch.setenv("YATING_API_KEY", "k")
    audio = np.ones(1600, dtype=np.float32)
    with patch("yating_stt.transcribe", new=AsyncMock(return_value="外匯車")):
        text, meta = await eng._run_yating_stt(audio)
    assert text == "外匯車"


@pytest.mark.asyncio
async def test_run_yating_exception_degrades(monkeypatch):
    eng = _engine()
    monkeypatch.setenv("YATING_API_KEY", "k")
    audio = np.ones(1600, dtype=np.float32)
    with patch("yating_stt.transcribe", new=AsyncMock(side_effect=RuntimeError("ws boom"))):
        assert await eng._run_yating_stt(audio) == ("", {})  # 不外漏，降級


# ── transcribe WS 協定：status 檢查（auth/quota 失敗秒降級，不卡 8s）─────────────

class _ACM:
    """最小 async context manager，__aenter__ 回傳指定值。"""

    def __init__(self, val):
        self.val = val

    async def __aenter__(self):
        return self.val

    async def __aexit__(self, *exc):
        return False


def _patch_yating_io(monkeypatch, ws):
    import sys
    fake_aiohttp = MagicMock()
    fake_aiohttp.ClientSession = MagicMock(return_value=_ACM(MagicMock()))
    fake_aiohttp.ClientTimeout = MagicMock()
    fake_ws_mod = MagicMock()
    fake_ws_mod.connect = MagicMock(return_value=_ACM(ws))
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)
    monkeypatch.setitem(sys.modules, "websockets", fake_ws_mod)
    monkeypatch.setattr(yating_stt, "_get_token", AsyncMock(return_value="tok"))


@pytest.mark.asyncio
async def test_transcribe_fast_fails_on_non_ok_status(monkeypatch):
    """雅婷回非 ok status（auth/quota 失敗）→ 立即回 ""，不送音訊、不卡到 timeout。"""
    ws = MagicMock()
    ws.recv = AsyncMock(return_value='{"status":"error","message":"quota exceeded"}')
    ws.send = AsyncMock()
    _patch_yating_io(monkeypatch, ws)
    out = await yating_stt.transcribe("key", b"\x00\x00" * 100, timeout=2.0)
    assert out == ""
    ws.send.assert_not_awaited()  # fast-fail：絕不送音訊


@pytest.mark.asyncio
async def test_transcribe_returns_text_on_ok_status(monkeypatch):
    """status ok → 送音訊、讀到 asr_final 回最終文字。"""
    ws = MagicMock()
    ws.recv = AsyncMock(side_effect=[
        '{"status":"ok"}',
        '{"pipe":{"asr_final":true,"asr_sentence":"外匯車"}}',
    ])
    ws.send = AsyncMock()
    _patch_yating_io(monkeypatch, ws)
    out = await yating_stt.transcribe("key", b"\x00\x00" * 100, timeout=2.0)
    assert out == "外匯車"
    assert ws.send.await_count >= 1
