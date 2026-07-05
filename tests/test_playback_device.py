"""
tests/test_playback_device.py

TDD 測試：DiscordPlaybackDevice 委派正確性 + _resolve_playback_device 解析點。
"""
from __future__ import annotations

from unittest.mock import MagicMock, call

from marvin_voice_core.playback_device import DiscordPlaybackDevice
from protocols import PlaybackDevice


# ── DiscordPlaybackDevice 委派正確性 ─────────────────────────────────────────

def _make_device():
    vc = MagicMock()
    vc.is_playing.return_value = False
    vc.is_connected.return_value = True
    return DiscordPlaybackDevice(vc), vc


def test_play_delegates_with_after():
    d, vc = _make_device()
    src = MagicMock()
    cb = MagicMock()
    d.play(src, after=cb)
    vc.play.assert_called_once_with(src, after=cb)


def test_play_delegates_without_after():
    d, vc = _make_device()
    src = MagicMock()
    d.play(src)
    vc.play.assert_called_once_with(src, after=None)


def test_is_playing_delegates_return_value():
    d, vc = _make_device()
    vc.is_playing.return_value = True
    assert d.is_playing() is True
    vc.is_playing.return_value = False
    assert d.is_playing() is False


def test_stop_calls_stop_playing_not_stop():
    """byte-equivalence: play_music calls vc.stop_playing(), not vc.stop()."""
    d, vc = _make_device()
    d.stop()
    vc.stop_playing.assert_called_once()
    vc.stop.assert_not_called()


def test_is_connected_delegates_return_value():
    d, vc = _make_device()
    vc.is_connected.return_value = True
    assert d.is_connected() is True
    vc.is_connected.return_value = False
    assert d.is_connected() is False


def test_runtime_checkable_isinstance():
    d, _ = _make_device()
    assert isinstance(d, PlaybackDevice)


# ── _resolve_playback_device 解析點 ──────────────────────────────────────────

def _resolve(voice_clients):
    """Call StateProxyMixin._resolve_playback_device via a minimal fake self."""
    from cogs.voice_controller_state_proxy import StateProxyMixin

    bot = MagicMock()
    bot.voice_clients = voice_clients
    fake_self = MagicMock(spec=StateProxyMixin)
    fake_self.bot = bot
    return StateProxyMixin._resolve_playback_device(fake_self)


def test_resolve_returns_device_when_connected_vc_exists():
    vc = MagicMock()
    vc.is_connected.return_value = True
    result = _resolve([vc])
    assert isinstance(result, DiscordPlaybackDevice)
    assert result._vc is vc


def test_resolve_returns_none_when_no_voice_clients():
    result = _resolve([])
    assert result is None


def test_resolve_returns_none_when_all_disconnected():
    vc = MagicMock()
    vc.is_connected.return_value = False
    result = _resolve([vc])
    assert result is None


def test_resolve_picks_first_connected():
    vc_off = MagicMock()
    vc_off.is_connected.return_value = False
    vc_on = MagicMock()
    vc_on.is_connected.return_value = True
    result = _resolve([vc_off, vc_on])
    assert isinstance(result, DiscordPlaybackDevice)
    assert result._vc is vc_on


# ── arm_mixer ────────────────────────────────────────────────────────────────

def test_arm_mixer_calls_play_with_audio_application():
    """arm_mixer 呼叫 vc.play(source, application='audio', bitrate=kbps)。"""
    d, vc = _make_device()
    vc.channel.bitrate = 128000  # 128 kbps
    src = MagicMock()
    d.arm_mixer(src)
    vc.play.assert_called_once_with(src, application="audio", bitrate=128)


def test_arm_mixer_bitrate_lower_bound():
    """bitrate 不得低於 16 kbps：8000 bps → 8//1000=0 → max(16,0)=16。"""
    d, vc = _make_device()
    vc.channel.bitrate = 8000
    src = MagicMock()
    d.arm_mixer(src)
    vc.play.assert_called_once_with(src, application="audio", bitrate=16)


def test_arm_mixer_bitrate_upper_bound():
    """bitrate 不得超過 512 kbps：600000 bps → 600//1000=600 → min(512,600)=512。"""
    d, vc = _make_device()
    vc.channel.bitrate = 600000
    src = MagicMock()
    d.arm_mixer(src)
    vc.play.assert_called_once_with(src, application="audio", bitrate=512)


def test_arm_mixer_bitrate_default_when_none():
    """channel.bitrate 為 None → 預設 128 kbps。"""
    d, vc = _make_device()
    vc.channel.bitrate = None
    src = MagicMock()
    d.arm_mixer(src)
    vc.play.assert_called_once_with(src, application="audio", bitrate=128)


def test_arm_mixer_bitrate_default_when_not_int():
    """channel.bitrate 非 int → 預設 128 kbps。"""
    d, vc = _make_device()
    vc.channel.bitrate = "128k"
    src = MagicMock()
    d.arm_mixer(src)
    vc.play.assert_called_once_with(src, application="audio", bitrate=128)


def test_raw_voice_client_removed():
    """raw_voice_client 接縫已移除：DiscordPlaybackDevice 不再有此屬性。"""
    d, _ = _make_device()
    assert not hasattr(d, "raw_voice_client"), "raw_voice_client 應已移除（③b 接縫拔除）"
