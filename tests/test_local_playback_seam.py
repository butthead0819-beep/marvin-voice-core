"""
tests/test_local_playback_seam.py

TDD 測試：local 模式輸出接縫（_local_mode / _local_speaker）。
先紅後綠（TDD）。

fake_self 刻意不用 spec=StateProxyMixin，以便自由設定 _local_mode/_local_speaker。
spec mock 的 _resolve 行為由 tests/test_playback_device.py 的 _resolve helper 覆蓋。
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

from cogs.voice_controller_state_proxy import StateProxyMixin
from marvin_voice_core.playback_device import DiscordPlaybackDevice, LocalSpeakerDevice


def _bot_with_vcs(voice_clients=()):
    bot = MagicMock()
    bot.voice_clients = list(voice_clients)
    return bot


def _connected_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    return vc


# ── (a) _local_mode=True + _local_speaker 設定 → 回傳注入的 device ─────────

def test_local_mode_with_real_speaker_device():
    """_local_mode=True 且 _local_speaker 是 LocalSpeakerDevice → 直接回傳，不掃 voice_clients。"""
    device = LocalSpeakerDevice(output=MagicMock())
    fake_self = MagicMock()
    fake_self._local_mode = True
    fake_self._local_speaker = device
    fake_self.bot = _bot_with_vcs()

    result = StateProxyMixin._resolve_playback_device(fake_self)
    assert result is device


def test_local_mode_with_mock_playback_device():
    """注入任何 PlaybackDevice-compatible mock → 是同一物件，不走 Discord 掃描。"""
    device = MagicMock()
    fake_self = MagicMock()
    fake_self._local_mode = True
    fake_self._local_speaker = device
    fake_self.bot = _bot_with_vcs()

    result = StateProxyMixin._resolve_playback_device(fake_self)
    assert result is device


# ── (b) _local_mode=True 但 _local_speaker=None → 退回掃描 voice_clients ────

def test_local_mode_no_speaker_connected_vc_falls_back():
    """_local_mode=True 但 _local_speaker=None → 退回掃描，回傳 DiscordPlaybackDevice。"""
    vc = _connected_vc()
    fake_self = MagicMock()
    fake_self._local_mode = True
    fake_self._local_speaker = None
    fake_self.bot = _bot_with_vcs([vc])

    result = StateProxyMixin._resolve_playback_device(fake_self)
    assert isinstance(result, DiscordPlaybackDevice)
    assert result._vc is vc


def test_local_mode_no_speaker_no_vc_returns_none():
    """_local_mode=True 但 _local_speaker=None 且無 vc → 回傳 None（不誤播）。"""
    fake_self = MagicMock()
    fake_self._local_mode = True
    fake_self._local_speaker = None
    fake_self.bot = _bot_with_vcs()

    result = StateProxyMixin._resolve_playback_device(fake_self)
    assert result is None


# ── (c) _local_mode=False（生產預設） → 與現行完全一致 ───────────────────────

def test_non_local_mode_connected_vc_returns_discord_device():
    """_local_mode=False → 有連線 vc 回傳 DiscordPlaybackDevice（字節等價）。"""
    vc = _connected_vc()
    fake_self = MagicMock()
    fake_self._local_mode = False
    fake_self.bot = _bot_with_vcs([vc])

    result = StateProxyMixin._resolve_playback_device(fake_self)
    assert isinstance(result, DiscordPlaybackDevice)
    assert result._vc is vc


def test_non_local_mode_no_vc_returns_none():
    """_local_mode=False 且無 vc → 回傳 None。"""
    fake_self = MagicMock()
    fake_self._local_mode = False
    fake_self.bot = _bot_with_vcs()

    result = StateProxyMixin._resolve_playback_device(fake_self)
    assert result is None


# ── (d) 屬性完全未設 → getattr default → Discord 分支 ────────────────────────

def test_no_local_attrs_getattr_default_falls_back_to_discord():
    """_local_mode/_local_speaker 屬性完全不存在 → getattr 取 default(False/None) → Discord 分支。"""
    vc = _connected_vc()
    bot = MagicMock()
    bot.voice_clients = [vc]
    # types.SimpleNamespace 只有 bot，沒有 _local_mode / _local_speaker
    fake_self = types.SimpleNamespace(bot=bot)

    result = StateProxyMixin._resolve_playback_device(fake_self)
    assert isinstance(result, DiscordPlaybackDevice)
    assert result._vc is vc


# ── set_local_speaker setter ─────────────────────────────────────────────────

def test_set_local_speaker_stores_device():
    """set_local_speaker(device) 設定 _local_speaker；後續 _resolve 能讀到。"""
    device = MagicMock()
    fake_self = MagicMock()
    fake_self._local_mode = True
    fake_self.bot = _bot_with_vcs()

    StateProxyMixin.set_local_speaker(fake_self, device)
    assert fake_self._local_speaker is device
