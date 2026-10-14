"""
TDD — WakeSessionMixin 接線（cogs/voice_controller_wake_session.py）。

_followup_override / _wake_session_on_replied / _wake_session_play_ack 用最小 fake
物件測；play_tts 掛鉤、handle_stt_result 端到端用真的 VoiceController（照
tests/test_playback_tts_path.py / tests/test_followup_listening.py 的 fixture 寫法）。
"""
from __future__ import annotations

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.voice_controller_wake_session import WakeSessionMixin, WAKE_SESSION_ACK_PATH
from wake_session import WakeSession


class _FakeCog(WakeSessionMixin):
    def __init__(self):
        self._wake_session = None
        self._mixer = None
        self._wake_response_pending = False


# ── _followup_override ───────────────────────────────────────────────────────

def test_followup_override_session_target_true_when_mixer_idle():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    with patch("time.time", return_value=1.0):
        assert cog._followup_override("A", None) is True


def test_followup_override_session_other_speaker_false():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    with patch("time.time", return_value=1.0):
        assert cog._followup_override("B", None) is False


def test_followup_override_false_when_mixer_speaking():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 1.0
    with patch("time.time", return_value=1.0):
        assert cog._followup_override("A", None) is False


def test_followup_override_legacy_fusion_window_d2a():
    cog = _FakeCog()
    cog._wake_session = None
    cog._wake_response_pending = False
    fusion = MagicMock()
    fusion.is_open.return_value = True
    assert cog._followup_override("A", fusion) is True

    cog._wake_response_pending = True
    assert cog._followup_override("A", fusion) is False


def test_followup_override_false_when_no_session_and_no_fusion():
    cog = _FakeCog()
    cog._wake_session = None
    cog._wake_response_pending = False
    assert cog._followup_override("A", None) is False


# ── _wake_session_on_replied ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_replied_creates_ack_task_on_transition(monkeypatch):
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session_play_ack = AsyncMock()
    with patch("time.time", return_value=0.5):
        cog._wake_session_on_replied()
    await asyncio.sleep(0)
    cog._wake_session_play_ack.assert_called_once_with("A")


@pytest.mark.asyncio
async def test_on_replied_no_new_task_when_already_listening(monkeypatch):
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session_play_ack = AsyncMock()
    with patch("time.time", return_value=0.5):
        cog._wake_session_on_replied()
    await asyncio.sleep(0)
    cog._wake_session_play_ack.reset_mock()
    with patch("time.time", return_value=1.0):
        cog._wake_session_on_replied()
    await asyncio.sleep(0)
    cog._wake_session_play_ack.assert_not_called()


@pytest.mark.asyncio
async def test_on_replied_respects_ack_env_off(monkeypatch):
    monkeypatch.setenv("MARVIN_WAKE_SESSION_ACK", "0")
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session_play_ack = AsyncMock()
    with patch("time.time", return_value=0.5):
        cog._wake_session_on_replied()
    await asyncio.sleep(0)
    cog._wake_session_play_ack.assert_not_called()


# ── _wake_session_play_ack ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_ack_plays_via_dj_layer_not_play_tts(monkeypatch):
    import cogs.voice_controller_wake_session as wsm
    monkeypatch.setattr(wsm, "_ACK_POLL_S", 0.001)
    monkeypatch.setattr(wsm, "_ACK_IDLE_REQUIRED_S", 0.002)
    monkeypatch.setattr(wsm, "_ACK_MAX_WAIT_S", 1.0)

    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog.play_dj_on_tts_layer = AsyncMock(return_value=True)
    cog.play_tts = AsyncMock()

    await cog._wake_session_play_ack("A")

    cog.play_dj_on_tts_layer.assert_called_once_with(WAKE_SESSION_ACK_PATH, peak=0.15)
    cog.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_play_ack_skips_when_session_transferred(monkeypatch):
    import cogs.voice_controller_wake_session as wsm
    monkeypatch.setattr(wsm, "_ACK_POLL_S", 0.001)
    monkeypatch.setattr(wsm, "_ACK_IDLE_REQUIRED_S", 0.002)
    monkeypatch.setattr(wsm, "_ACK_MAX_WAIT_S", 1.0)

    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._wake_session.on_wake_accepted("B", 1.0)   # session 轉給別人
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog.play_dj_on_tts_layer = AsyncMock(return_value=True)

    await cog._wake_session_play_ack("A")

    cog.play_dj_on_tts_layer.assert_not_called()


@pytest.mark.asyncio
async def test_play_ack_skips_when_no_mixer(monkeypatch):
    import cogs.voice_controller_wake_session as wsm
    monkeypatch.setattr(wsm, "_ACK_POLL_S", 0.001)
    monkeypatch.setattr(wsm, "_ACK_IDLE_REQUIRED_S", 0.002)
    monkeypatch.setattr(wsm, "_ACK_MAX_WAIT_S", 1.0)

    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = None
    cog.play_dj_on_tts_layer = AsyncMock(return_value=True)

    await cog._wake_session_play_ack("A")

    cog.play_dj_on_tts_layer.assert_not_called()


# ── play_tts 掛鉤 ──────────────────────────────────────────────────────────────

from tests.test_playback_tts_path import _make_cog as _make_playback_cog
from marvin_voice_core.playback_device import DiscordPlaybackDevice
from tts_speak_policy import SpeakKind


@pytest.mark.asyncio
async def test_play_tts_legacy_kind_wake_reply_triggers_wake_session_replied():
    cog, _vc = _make_playback_cog()
    device = MagicMock(spec=DiscordPlaybackDevice)
    cog._wake_session_on_replied = MagicMock()
    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("測試文字")   # kind=None → legacy → WAKE_REPLY
    cog._wake_session_on_replied.assert_called_once()


@pytest.mark.asyncio
async def test_play_tts_dj_narration_kind_does_not_trigger_wake_session_replied():
    cog, _vc = _make_playback_cog()
    device = MagicMock(spec=DiscordPlaybackDevice)
    cog._wake_session_on_replied = MagicMock()
    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("口白文字", kind=SpeakKind.DJ_NARRATION)
    cog._wake_session_on_replied.assert_not_called()


@pytest.mark.asyncio
async def test_play_tts_zero_pushed_does_not_trigger_wake_session_replied():
    cog, _vc = _make_playback_cog()
    device = MagicMock(spec=DiscordPlaybackDevice)
    cog._stream_tts_to_mixer = AsyncMock(return_value=0)
    cog._wake_session_on_replied = MagicMock()
    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("測試文字")
    cog._wake_session_on_replied.assert_not_called()


# ── 端到端：handle_stt_result 經 WakeSession 免喚醒 ───────────────────────────

from tests.test_followup_listening import _make_cog_for_stt


@pytest.mark.asyncio
async def test_handle_stt_result_wake_session_target_bypasses_wake_word():
    cog, wf = _make_cog_for_stt()
    wf.is_open.return_value = False   # 關掉舊 follow-up 窗，只測 WakeSession
    cog._wake_session = WakeSession()
    _t0 = time.time()   # handle_stt_result 用真實時鐘 → session 時間軸要對齊，否則被判閒置超時
    cog._wake_session.on_wake_accepted("TestUser", _t0)
    cog._wake_session.on_replied(_t0)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0

    await cog.handle_stt_result(
        "TestUser", "要", time.time(), b"fake_audio",
        bypass_etd=True,
    )
    assert cog._wake_response_pending is True


@pytest.mark.asyncio
async def test_handle_stt_result_wake_session_other_speaker_still_needs_wake_word():
    cog, wf = _make_cog_for_stt()
    wf.is_open.return_value = False
    cog._wake_session = WakeSession()
    _t0 = time.time()   # handle_stt_result 用真實時鐘 → session 時間軸要對齊，否則被判閒置超時
    cog._wake_session.on_wake_accepted("TestUser", _t0)
    cog._wake_session.on_replied(_t0)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0

    await cog.handle_stt_result(
        "OtherUser", "要", time.time(), b"fake_audio",
        bypass_etd=True,
    )
    assert cog._wake_response_pending is False
