"""SwiftV2 lane — SpeechAnalyzer 新引擎路由（2026-06-13）。

spike + A/B 實證：v2（macOS 26 SpeechAnalyzer）完整性壓倒 v1
（v1 丟「馬文」/砍半句；v2 全保留、239-884ms on-device），同音字變體
（週傑倫/鉆石）交給下游 cleaner+corrections 正規化。

佈署形態：STT_ENGINE_V2 閘控，v2 當全句主力，空輸出自動降 v1→Groq
既有鏈（優雅降級）；wake-check 維持 v1（latency 關鍵 + 久經實戰）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_bot():
    bot = MagicMock()
    bot.router = MagicMock()
    bot.router.game_dict_string = ""
    bot.get_cog.return_value = None
    return bot


def _make_engine():
    from discord_voice_engine import DiscordVoiceEngine
    return DiscordVoiceEngine(_make_bot())


def _mock_subprocess(monkeypatch, stdout=b"__META__ {}\n\xe4\xbd\xa0\xe5\xa5\xbd"):
    """攔截 create_subprocess_exec，回傳 (captured_args, AsyncMock)。"""
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        return proc

    import asyncio
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


@pytest.mark.asyncio
async def test_v2_flag_selects_v2_binary(monkeypatch):
    engine = _make_engine()
    captured = _mock_subprocess(monkeypatch)

    text, _ = await engine._run_swift_stt("/tmp/x.wav", is_wake_check=False, v2=True)

    assert captured["args"][0] == "./macos_stt_v2_bin"
    assert text == "你好"


@pytest.mark.asyncio
async def test_default_uses_v1_binary(monkeypatch):
    engine = _make_engine()
    captured = _mock_subprocess(monkeypatch)

    await engine._run_swift_stt("/tmp/x.wav", is_wake_check=False)

    assert captured["args"][0] == "./macos_stt_bin"


@pytest.mark.asyncio
async def test_wake_check_never_uses_v2(monkeypatch):
    """wake-check 路徑維持 v1（latency 關鍵；v2 不收 --wake-check）。"""
    engine = _make_engine()
    captured = _mock_subprocess(monkeypatch)

    await engine._run_swift_stt("/tmp/x.wav", is_wake_check=True)

    assert captured["args"][0] == "./macos_stt_bin"
    assert "--wake-check" in captured["args"]


def test_build_stt_context_gathers_songs_and_speakers():
    engine = _make_engine()
    vc = MagicMock()
    vc._current_stream_info = {"title": "晴天 Official MV"}
    vc.stream_queue = [{"title": "夜曲"}]
    vc._parse_song_title_artist.side_effect = lambda info: (info["title"].split()[0], "周杰倫")
    engine.bot.get_cog.return_value = vc
    engine.conv_buffer = MagicMock()
    engine.conv_buffer.get_active_speakers.return_value = {"狗與露"}

    ctx = engine._build_stt_context()

    parts = ctx.split(",")
    for expected in ("馬文", "晴天", "夜曲", "周杰倫", "狗與露"):
        assert expected in parts
    assert engine._last_stt_context == ctx
