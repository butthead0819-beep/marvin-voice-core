"""喚醒成功但 LLM 久候未出聲 → Marvin 語音 ack 回報狀態（安撫 UX）。

設計：
- 自然間隔 ≥5s 才有 ack 價值（<5s 插話只是吵）→ 5s 才第一發
- 雙發：5s first / 12s second（escalation）
- 出首句音訊前才播；已出聲立即收手
- 4 狀態（thinking / searching / busy / fallback）各對應預渲染 mp3
- 狀態偵測在「開播當下」決定，反映真實當下系統狀態

本測試只驗狀態機行為與播放序列化，不驗 edge-tts 內容。
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
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
    return cog


# ── 狀態偵測 ────────────────────────────────────────────────────────────────

def test_detect_state_defaults_to_thinking():
    cog = _make_cog()
    cog._llm_searching = False
    cog._last_fallback_ts = 0.0
    assert cog._detect_llm_wait_state() == "thinking"


def test_detect_state_searching_when_search_flag_set():
    cog = _make_cog()
    cog._llm_searching = True
    cog._last_fallback_ts = 0.0
    assert cog._detect_llm_wait_state() == "searching"


def test_detect_state_fallback_when_recent_fallback():
    cog = _make_cog()
    cog._llm_searching = False
    cog._last_fallback_ts = time.time()  # 剛剛降級
    assert cog._detect_llm_wait_state() == "fallback"


def test_detect_state_fallback_ignored_when_stale():
    cog = _make_cog()
    cog._llm_searching = False
    cog._last_fallback_ts = time.time() - 999  # 很久以前，已不算降級中
    assert cog._detect_llm_wait_state() == "thinking"


def test_detect_state_busy_when_bus_recently_degraded():
    cog = _make_cog()
    cog._llm_searching = False
    cog._last_fallback_ts = 0.0
    bus = MagicMock()
    bus._last_degraded_ts = time.monotonic()  # 剛告警 provider 短缺
    cog.bot.router._llm_bus = bus
    assert cog._detect_llm_wait_state() == "busy"


def test_detect_state_searching_takes_precedence_over_fallback():
    cog = _make_cog()
    cog._llm_searching = True
    cog._last_fallback_ts = time.time()
    assert cog._detect_llm_wait_state() == "searching"


# ── ack 播放（序列化 + 跳過條件）─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_status_ack_acquires_playback_lock(tmp_path):
    cog = _make_cog()
    fake_vc = MagicMock()
    fake_vc.is_connected.return_value = True
    fake_vc.is_playing.return_value = False
    cog.voice_client = fake_vc

    lock_held = {"value": False}
    real_play = MagicMock()

    def _spy_play(*a, **kw):
        lock_held["value"] = cog.playback_lock.locked()
        return real_play(*a, **kw)
    fake_vc.play = MagicMock(side_effect=_spy_play)

    ack = tmp_path / "thinking_first_1.mp3"
    ack.write_bytes(b"fake")

    with patch("glob.glob", return_value=[str(ack)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_status_ack("thinking", "first")

    assert fake_vc.play.called
    assert lock_held["value"] is True


@pytest.mark.asyncio
async def test_play_status_ack_skips_when_already_playing():
    cog = _make_cog()
    fake_vc = MagicMock()
    fake_vc.is_connected.return_value = True
    fake_vc.is_playing.return_value = True
    fake_vc.play = MagicMock()
    cog.voice_client = fake_vc
    cog.is_playing_audio = True

    await cog._play_status_ack("thinking", "first")
    assert not fake_vc.play.called


@pytest.mark.asyncio
async def test_play_status_ack_uses_hotswap_during_music(tmp_path, monkeypatch):
    """串流播歌中 + 熱切換開啟 → ack 走 _arm_hotswap 注入，不走 plain play（不打斷音樂）。"""
    cog = _make_cog()
    fake_vc = MagicMock()
    fake_vc.is_connected.return_value = True
    fake_vc.is_playing.return_value = True  # 音樂正在播
    fake_vc.play = MagicMock()
    cog.voice_client = fake_vc

    # _midsong_hotswap_active 的真實前置條件
    monkeypatch.setenv("MARVIN_MIDSONG_HOTSWAP_ENABLED", "true")
    cog.stream_mode = True
    cog._stream_position_source = object()
    cog._current_stream_url = "http://example/stream"
    cog._arm_hotswap = AsyncMock(return_value=True)

    ack = tmp_path / "searching_first_1.mp3"
    ack.write_bytes(b"fake")

    with patch("glob.glob", return_value=[str(ack)]):
        await cog._play_status_ack("searching", "first")

    cog._arm_hotswap.assert_called_once_with(str(ack))
    assert not fake_vc.play.called, "音樂中應走熱切換，不可 plain play 打斷音樂"


@pytest.mark.asyncio
async def test_play_status_ack_skips_when_no_files(tmp_path):
    cog = _make_cog()
    fake_vc = MagicMock()
    fake_vc.is_connected.return_value = True
    fake_vc.is_playing.return_value = False
    fake_vc.play = MagicMock()
    cog.voice_client = fake_vc

    with patch("glob.glob", return_value=[]):
        await cog._play_status_ack("thinking", "first")
    assert not fake_vc.play.called


# ── watcher 雙發 / 提早收手 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watcher_double_fire_when_no_audio():
    """5s 第一發、12s 第二發；全程沒出首句音訊 → 兩發都打。"""
    cog = _make_cog()
    fired = []
    cog._play_status_ack = AsyncMock(side_effect=lambda s, t: fired.append((s, t)))
    cog._detect_llm_wait_state = MagicMock(return_value="thinking")

    with patch("asyncio.sleep", new=AsyncMock()):
        await cog._llm_wait_ack_watcher(lambda: False)

    assert fired == [("thinking", "first"), ("thinking", "second")]


@pytest.mark.asyncio
async def test_watcher_no_fire_when_audio_arrives_before_5s():
    """首句在 5s 前就到 → 一發都不打。"""
    cog = _make_cog()
    cog._play_status_ack = AsyncMock()
    cog._detect_llm_wait_state = MagicMock(return_value="thinking")

    with patch("asyncio.sleep", new=AsyncMock()):
        await cog._llm_wait_ack_watcher(lambda: True)

    assert not cog._play_status_ack.called


@pytest.mark.asyncio
async def test_watcher_only_first_when_audio_arrives_after_5s():
    """首句在 5s~12s 之間到 → 只打第一發，不打第二發。"""
    cog = _make_cog()
    cog._play_status_ack = AsyncMock()
    cog._detect_llm_wait_state = MagicMock(return_value="searching")

    state = {"audio": False, "calls": 0}

    async def _fake_sleep(_):
        # 第一次 sleep(5) 後 audio 仍 False（打第一發）；
        # 第二次 sleep 後翻成 True（攔第二發）
        state["calls"] += 1
        if state["calls"] >= 2:
            state["audio"] = True
    with patch("asyncio.sleep", new=AsyncMock(side_effect=_fake_sleep)):
        await cog._llm_wait_ack_watcher(lambda: state["audio"])

    cog._play_status_ack.assert_called_once_with("searching", "first")


@pytest.mark.asyncio
async def test_watcher_cancellable():
    """被 cancel 時安靜結束，不外漏 CancelledError。"""
    cog = _make_cog()
    cog._play_status_ack = AsyncMock()

    real_sleep = asyncio.sleep  # patch 前保存，供測試自己讓步用

    async def _hang(_):
        await asyncio.Event().wait()  # 永遠不醒
    with patch("asyncio.sleep", new=AsyncMock(side_effect=_hang)):
        task = asyncio.create_task(cog._llm_wait_ack_watcher(lambda: False))
        await real_sleep(0)  # 讓 watcher 跑到第一個（被攔住的）sleep
        task.cancel()
        await task  # 不應 raise
    assert not cog._play_status_ack.called
