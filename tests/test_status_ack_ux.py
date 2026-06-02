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


# ── watcher 雙發 / 提早收手 ──────────────────────────────────────────────────
# （ack 實際播放政策由 test_play_ack_unified.py 的 status case 覆蓋；
#   這裡只驗 watcher 的計時/收手/狀態組裝邏輯。）

@pytest.mark.asyncio
async def test_watcher_double_fire_when_no_audio():
    """5s 第一發、12s 第二發；全程沒出首句音訊 → 兩發都打，variant 帶 state。"""
    cog = _make_cog()
    fired = []
    cog._play_ack = AsyncMock(side_effect=lambda c, variant=None: fired.append((c, variant)))
    cog._detect_llm_wait_state = MagicMock(return_value="thinking")

    with patch("asyncio.sleep", new=AsyncMock()):
        await cog._llm_wait_ack_watcher(lambda: False)

    assert fired == [("status", "thinking_first"), ("status", "thinking_second")]


@pytest.mark.asyncio
async def test_watcher_no_fire_when_audio_arrives_before_5s():
    """首句在 5s 前就到 → 一發都不打。"""
    cog = _make_cog()
    cog._play_ack = AsyncMock()
    cog._detect_llm_wait_state = MagicMock(return_value="thinking")

    with patch("asyncio.sleep", new=AsyncMock()):
        await cog._llm_wait_ack_watcher(lambda: True)

    assert not cog._play_ack.called


@pytest.mark.asyncio
async def test_watcher_only_first_when_audio_arrives_after_5s():
    """首句在 5s~12s 之間到 → 只打第一發，不打第二發。"""
    cog = _make_cog()
    cog._play_ack = AsyncMock()
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

    cog._play_ack.assert_called_once_with("status", variant="searching_first")


@pytest.mark.asyncio
async def test_watcher_cancellable():
    """被 cancel 時安靜結束，不外漏 CancelledError。"""
    cog = _make_cog()
    cog._play_ack = AsyncMock()

    real_sleep = asyncio.sleep  # patch 前保存，供測試自己讓步用

    async def _hang(_):
        await asyncio.Event().wait()  # 永遠不醒
    with patch("asyncio.sleep", new=AsyncMock(side_effect=_hang)):
        task = asyncio.create_task(cog._llm_wait_ack_watcher(lambda: False))
        await real_sleep(0)  # 讓 watcher 跑到第一個（被攔住的）sleep
        task.cancel()
        await task  # 不應 raise
    assert not cog._play_ack.called
