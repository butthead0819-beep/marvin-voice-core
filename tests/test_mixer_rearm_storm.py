"""☢️ [Plan12 Mixer] 自癒重武裝無窮迴圈 → voice WS SPEAKING 洪水 → Discord 4021 踢線。

2026-09-17 事故（真根因，找了一整天）：commit 2887276 給 arm_mixer 掛了 after= 自癒
callback，本意是「AudioPlayer thread 靜默死掉沒人通知」。但 on-demand mixer 閒置超過
grace 時 read() 回 b"" 是**設計上的正常停送**（見 LocalMixingAudioSource.read 的
`if self._on_demand: ... return b""`），不是故障。discord.py AudioPlayer._do_run()
拿到 b"" 就 self.stop() → _call_after() → 我們無條件重武裝 → 新 player → 又讀到 b""
→ ... 無窮迴圈。

每一輪迴圈送 2 個 voice websocket SPEAKING(op 5)：
  _do_run 開頭 self._speak(SpeakingState.voice) + stop() 裡 self._speak(SpeakingState.none)
實測 stdout：`[Plan12_Mixer] adapter armed` 在一分鐘內出現 223 次、彼此間隔約 1ms，
等於每秒上千個 op-5 打進語音 websocket → Discord 回 4021（RateLimited）踢線。

對照組（退版 4b44ee5，2887276 之前）：同一段時間窗只有 3 次 armed，連線穩定不被踢。
這解釋了「舊版可以連、新版秒被踢」以及「限流 17 小時不解除」——每次連上都在幾秒內
自己把自己洪水打進限流，不是 Discord 端在罰我們。

規則：
1. mixer 閒置 → 這是正常停送，**不重武裝**（對齊 sentinel_monitor_loop 既有的
   `not self._mixer.is_idle()` 判斷）。
2. 還有內容卻停了 → 才是 2887276 要救的真故障，重武裝。
3. 無論如何加最小間隔防抖，讓「緊迴圈」在結構上不可能再發生。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from marvin_voice_core.playback_device import DiscordPlaybackDevice


def _make_cog(monkeypatch):
    monkeypatch.setenv("PLAN12_LOCAL_MIX", "true")
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
    # 模擬 event loop：call_soon_threadsafe 直接同步執行，重現 player thread → loop 的排程
    bot.loop.is_closed.return_value = False
    bot.loop.call_soon_threadsafe.side_effect = lambda fn, *a: fn(*a)
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        bot.engine = DiscordVoiceEngine(bot)
    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        return VoiceController(bot)


def _idle_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False  # player 結束後就是 False → 不擋重武裝
    return vc


def _arm_and_get_after(cog, vc):
    """跑一次 _ensure_mixer_playing，取回交給 discord.py 的 after callback。"""
    device = DiscordPlaybackDevice(vc)
    assert cog._ensure_mixer_playing(device) is True
    return vc.play.call_args.kwargs["after"], device


# ── 核心回歸：idle 停送不可重武裝（否則無窮迴圈 → 4021）────────────────────────

def test_idle_mixer_after_callback_does_not_rearm(monkeypatch):
    """mixer 閒置回 b"" 是正常停送 → after 不該重武裝（這正是 4021 洪水的源頭）。"""
    cog = _make_cog(monkeypatch)
    vc = _idle_vc()
    after, _device = _arm_and_get_after(cog, vc)
    assert cog._mixer.is_idle() is True  # 新 mixer 沒內容
    after(None)
    assert vc.play.call_count == 1  # 只有最初那次，沒有第二次重武裝


def test_idle_mixer_repeated_after_never_storms(monkeypatch):
    """重現事故本體：連續 200 次 after 回呼（等同 player 一直讀到 b""）不該產生風暴。"""
    cog = _make_cog(monkeypatch)
    vc = _idle_vc()
    after, _device = _arm_and_get_after(cog, vc)
    for _ in range(200):
        after(None)
    assert vc.play.call_count == 1


# ── 保住 2887276 的原意：真故障（還有內容卻停了）仍要自癒 ──────────────────────

def test_non_idle_mixer_after_callback_rearms(monkeypatch):
    """mixer 還有內容卻停了 = AudioPlayer thread 真的死了 → 要重武裝（原修復意圖）。"""
    cog = _make_cog(monkeypatch)
    vc = _idle_vc()
    after, _device = _arm_and_get_after(cog, vc)
    with patch.object(cog._mixer, "is_idle", return_value=False):
        after(None)
    assert vc.play.call_count == 2


# ── 結構性防線：最小間隔防抖，緊迴圈在設計上不可能 ────────────────────────────

def test_rearm_debounced_within_min_interval(monkeypatch):
    """同一秒內連續觸發自癒 → 只允許一次，避免任何殘留路徑再變成緊迴圈。"""
    cog = _make_cog(monkeypatch)
    vc = _idle_vc()
    after, _device = _arm_and_get_after(cog, vc)
    with patch.object(cog._mixer, "is_idle", return_value=False):
        after(None)
        after(None)
        after(None)
    assert vc.play.call_count == 2  # 初次 + 一次自癒，後兩次被防抖擋掉


def test_rearm_allowed_again_after_min_interval(monkeypatch):
    """超過最小間隔後，自癒要能再次生效（防抖不能變成永久封印）。"""
    cog = _make_cog(monkeypatch)
    vc = _idle_vc()
    after, _device = _arm_and_get_after(cog, vc)
    with patch.object(cog._mixer, "is_idle", return_value=False):
        after(None)
        cog._last_mixer_rearm_ts -= (cog._MIXER_REARM_MIN_INTERVAL_S + 1.0)  # 時間快轉
        after(None)
    assert vc.play.call_count == 3
