"""統一 _play_ack(category) — 收編 _play_ack_sound / _nemoclaw / _status 三分支。

驗每個 category 的播放政策忠實對應舊行為：
- wake：prewarm TTS、從 wake_zh/en pool、無檔走 text_fallback
- music：urgent 熱切換、子 pool 空退回 wake、await_completion
- music_fail：非 urgent（音樂中不切）
- nemoclaw / status：走 lock、播放中跳過
- filler：不鎖、僅空檔插隊
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
    bot.tts_engine.prewarm = AsyncMock()
    bot.router = MagicMock()
    bot.router._llm_bus = None

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog._speaker_lang = {}
    return cog


def _idle_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    # await_completion 路徑：play(after=...) 立即觸發 after 讓 ack_done set
    def _play(src, after=None):
        if after:
            after(None)
    vc.play = MagicMock(side_effect=_play)
    return vc


# ── wake ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wake_prewarms_and_plays_from_wake_pool(tmp_path):
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    f = tmp_path / "ack_1.mp3"; f.write_bytes(b"x")

    with patch("glob.glob", return_value=[str(f)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_ack("wake", speaker="阿狗")

    assert cog.bot.tts_engine.prewarm.called   # wake 預告回應 → 暖 TTS
    assert vc.play.called


@pytest.mark.asyncio
async def test_wake_no_files_uses_text_fallback():
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    cog.play_tts = AsyncMock()

    with patch("glob.glob", return_value=[]):
        await cog._play_ack("wake", speaker="阿狗")

    assert cog.play_tts.called          # 連檔都沒 → 即時合成
    assert not vc.play.called


@pytest.mark.asyncio
async def test_wake_en_globs_en_pool():
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    cog._speaker_lang = {"Bob": "en"}
    seen = {}

    def _glob(pat):
        seen["pat"] = pat
        return []
    with patch("glob.glob", side_effect=_glob):
        cog.play_tts = AsyncMock()
        await cog._play_ack("wake", speaker="Bob")

    assert "assets/acks_en" in seen["pat"]


# ── music ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_music_empty_falls_back_to_wake_pool(tmp_path):
    """music pool 空 → 退回 wake_zh pool 取檔。"""
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    wake_file = tmp_path / "ack_3.mp3"; wake_file.write_bytes(b"x")

    def _glob(pat):
        if "acks/music" in pat:
            return []                 # music pool 空
        if pat == "assets/acks/*.mp3":
            return [str(wake_file)]    # wake pool 有檔
        return []
    with patch("glob.glob", side_effect=_glob), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_ack("music", speaker="阿狗")

    assert vc.play.called


@pytest.mark.asyncio
async def test_music_uses_hotswap_during_music(tmp_path, monkeypatch):
    cog = _make_cog()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True
    vc.play = MagicMock()
    cog.voice_client = vc

    monkeypatch.setenv("MARVIN_MIDSONG_HOTSWAP_ENABLED", "true")
    cog.stream_mode = True
    cog._stream_position_source = object()
    cog._current_stream_url = "http://x"
    cog._arm_hotswap = AsyncMock(return_value=True)

    f = tmp_path / "music_ack_01.mp3"; f.write_bytes(b"x")
    with patch("glob.glob", return_value=[str(f)]):
        await cog._play_ack("music", speaker="阿狗")

    cog._arm_hotswap.assert_called_once_with(str(f))
    assert not vc.play.called          # 音樂中走熱切換，不 plain play


@pytest.mark.asyncio
async def test_music_fail_no_hotswap(tmp_path, monkeypatch):
    """music_fail 非 urgent：即使在音樂中也不走熱切換。"""
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    monkeypatch.setenv("MARVIN_MIDSONG_HOTSWAP_ENABLED", "true")
    cog.stream_mode = True
    cog._stream_position_source = object()
    cog._current_stream_url = "http://x"
    cog._arm_hotswap = AsyncMock(return_value=True)

    f = tmp_path / "music_fail.mp3"; f.write_bytes(b"x")
    with patch("glob.glob", return_value=[str(f)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_ack("music_fail", speaker="阿狗")

    assert not cog._arm_hotswap.called   # 非 urgent → 不熱切換


# ── nemoclaw / status：lock + skip_if_busy ──────────────────────────────────

@pytest.mark.asyncio
async def test_nemoclaw_skips_when_busy():
    cog = _make_cog()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True
    vc.play = MagicMock()
    cog.voice_client = vc
    cog.is_playing_audio = True

    await cog._play_ack("nemoclaw", speaker="阿狗")
    assert not vc.play.called


@pytest.mark.asyncio
async def test_nemoclaw_holds_lock_when_idle(tmp_path):
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    held = {"v": False}
    real = vc.play
    def _spy(*a, **k):
        held["v"] = cog.playback_lock.locked()
        return real(*a, **k)
    vc.play = MagicMock(side_effect=_spy)

    f = tmp_path / "ack_1.mp3"; f.write_bytes(b"x")
    with patch("glob.glob", return_value=[str(f)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_ack("nemoclaw", speaker="阿狗")

    assert vc.play.called
    assert held["v"] is True


@pytest.mark.asyncio
async def test_status_variant_globs_prefix(tmp_path):
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    seen = {}
    def _glob(pat):
        seen["pat"] = pat
        return []
    with patch("glob.glob", side_effect=_glob):
        await cog._play_ack("status", variant="searching_first")
    assert seen["pat"] == "assets/acks_status/searching_first_*.mp3"


# ── filler：不鎖、僅空檔 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filler_skips_when_playing():
    cog = _make_cog()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True
    vc.play = MagicMock()
    cog.voice_client = vc

    await cog._play_ack("filler", speaker="阿狗")
    assert not vc.play.called


@pytest.mark.asyncio
async def test_filler_plays_without_lock_when_idle(tmp_path):
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    held = {"v": True}
    real = vc.play
    def _spy(*a, **k):
        held["v"] = cog.playback_lock.locked()
        return real(*a, **k)
    vc.play = MagicMock(side_effect=_spy)

    f = tmp_path / "ack_1.mp3"; f.write_bytes(b"x")
    with patch("glob.glob", return_value=[str(f)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_ack("filler", speaker="阿狗")

    assert vc.play.called
    assert held["v"] is False          # filler 故意不鎖
