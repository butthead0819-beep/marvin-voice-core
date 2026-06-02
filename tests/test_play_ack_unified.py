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


# ── 主動 ack appropriateness gate（意圖導向）──────────────────────────────────
import ack_templates as A
import time as _t


def _recent(cog, *texts, age_s=1.0):
    """讓 conv_buffer 回傳近窗內的這幾句。"""
    ts = _t.time() - age_s
    cog.bot.engine.conv_buffer = MagicMock()
    cog.bot.engine.conv_buffer.get_last_n_utterances.return_value = [
        {"text": x, "timestamp": ts} for x in texts
    ]


_STATUS = A.CATEGORIES["status"]
_FILLER = A.CATEGORIES["filler"]


def test_is_status_probe_matches_complaints():
    cog = _make_cog()
    for t in ["怎麼都沒反應", "Marvin 還在嗎", "壞了喔", "好了沒", "喂？", "hello?"]:
        assert cog._is_status_probe(t) is True, t


def test_is_status_probe_rejects_chitchat():
    cog = _make_cog()
    for t in ["今天天氣真好", "你昨天看球賽了嗎", "我想點周杰倫"]:
        assert cog._is_status_probe(t) is False, t


def test_gate_blocks_in_echo_cooldown():
    cog = _make_cog()
    cog._tts_echo_cooldown_until = _t.time() + 2.0
    _recent(cog)  # 無近窗文字
    assert cog._active_ack_allowed(_STATUS) is False


def test_gate_allows_when_silent():
    cog = _make_cog()
    cog._tts_echo_cooldown_until = 0.0
    _recent(cog)  # 近窗沒人講 → 安靜等待
    assert cog._active_ack_allowed(_STATUS) is True


def test_gate_allows_on_status_probe():
    cog = _make_cog()
    cog._tts_echo_cooldown_until = 0.0
    _recent(cog, "怎麼都沒反應")   # 在問狀態 → 立刻放
    assert cog._active_ack_allowed(_STATUS) is True


def test_gate_suppresses_on_chitchat():
    cog = _make_cog()
    cog._tts_echo_cooldown_until = 0.0
    _recent(cog, "今天天氣真好")   # 閒聊 → 壓住
    assert cog._active_ack_allowed(_STATUS) is False


def test_gate_ignores_stale_text_outside_window():
    cog = _make_cog()
    cog._tts_echo_cooldown_until = 0.0
    _recent(cog, "今天天氣真好", age_s=99)  # 太舊（窗外）→ 視同沉默 → 放
    assert cog._active_ack_allowed(_STATUS) is True


def test_filler_gate_ignores_intent():
    """filler 非 intent_aware：閒聊也不影響，只看 echo 窗。"""
    cog = _make_cog()
    cog._tts_echo_cooldown_until = 0.0
    _recent(cog, "今天天氣真好")
    assert cog._active_ack_allowed(_FILLER) is True
    cog._tts_echo_cooldown_until = _t.time() + 2.0
    assert cog._active_ack_allowed(_FILLER) is False


@pytest.mark.asyncio
async def test_status_suppressed_during_chitchat(tmp_path):
    """status：使用者在閒聊 → 不放（連熱切換都不注入）。"""
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    cog._tts_echo_cooldown_until = 0.0
    _recent(cog, "你昨天看球賽了嗎")
    cog._arm_hotswap = AsyncMock(return_value=True)

    f = tmp_path / "thinking_first_1.mp3"; f.write_bytes(b"x")
    with patch("glob.glob", return_value=[str(f)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_ack("status", variant="thinking_first")

    assert not vc.play.called
    assert not cog._arm_hotswap.called


@pytest.mark.asyncio
async def test_status_fires_on_probe(tmp_path):
    """status：使用者問「沒反應?」→ 立刻放。"""
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    cog._tts_echo_cooldown_until = 0.0
    _recent(cog, "怎麼都沒反應")

    f = tmp_path / "thinking_first_1.mp3"; f.write_bytes(b"x")
    with patch("glob.glob", return_value=[str(f)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_ack("status", variant="thinking_first")

    assert vc.play.called


@pytest.mark.asyncio
async def test_passive_ack_ignores_active_gate(tmp_path):
    """wake（被動）：即使閒聊 / echo 窗，仍照放（被動一定該確認）。"""
    cog = _make_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    cog._tts_echo_cooldown_until = _t.time() + 5.0
    _recent(cog, "今天天氣真好")

    f = tmp_path / "ack_1.mp3"; f.write_bytes(b"x")
    with patch("glob.glob", return_value=[str(f)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_ack("wake", speaker="阿狗")

    assert vc.play.called


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
