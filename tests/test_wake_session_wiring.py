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


# ── _room_crowded 接線（2026-10-24 五人熱聊事故）────────────────────────────


def test_followup_override_crowded_true_blocks_target():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog._room_crowded = MagicMock(return_value=True)
    with patch("time.time", return_value=1.0):
        assert cog._followup_override("A", None) is False
    assert cog._wake_session.target == "A"


def test_followup_override_crowded_false_allows_target():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog._room_crowded = MagicMock(return_value=False)
    with patch("time.time", return_value=1.0):
        assert cog._followup_override("A", None) is True


def test_followup_override_non_target_does_not_check_crowded():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog._room_crowded = MagicMock(return_value=True)
    with patch("time.time", return_value=1.0):
        assert cog._followup_override("B", None) is False
    cog._room_crowded.assert_not_called()


def test_room_crowded_default_false_without_bot_attribute():
    cog = _FakeCog()
    assert cog._room_crowded("A") is False


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
    monkeypatch.setattr(wsm, "_ACK_SPEECH_WAIT_S", 0.001)

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
    monkeypatch.setattr(wsm, "_ACK_SPEECH_WAIT_S", 0.001)

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
    monkeypatch.setattr(wsm, "_ACK_SPEECH_WAIT_S", 0.001)

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


@pytest.mark.asyncio
async def test_play_ack_skips_when_new_turn_already_captured(monkeypatch):
    """等播完期間對象已經又講了一句被接住（awaiting）→ 不在他講話/等回應時插提示音。"""
    import cogs.voice_controller_wake_session as wsm
    monkeypatch.setattr(wsm, "_ACK_POLL_S", 0.001)
    monkeypatch.setattr(wsm, "_ACK_IDLE_REQUIRED_S", 0.002)
    monkeypatch.setattr(wsm, "_ACK_MAX_WAIT_S", 1.0)
    monkeypatch.setattr(wsm, "_ACK_SPEECH_WAIT_S", 0.001)

    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    assert cog._wake_session.on_utterance("A", 1.0, marvin_speaking=False) is True
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog.play_dj_on_tts_layer = AsyncMock(return_value=True)

    await cog._wake_session_play_ack("A")

    cog.play_dj_on_tts_layer.assert_not_called()


def test_apply_wake_guards_accepted_wake_arms_session():
    """真 VoiceController._apply_wake_guards：接受喚醒 → session arm 給該 speaker。"""
    from tests.test_wake_guards_unit import _make_vc
    vc = _make_vc(_wake_session=WakeSession(), _mixer=None)
    is_fast, _, _ = vc._apply_wake_guards(
        "A", "馬文查周杰倫新歌", time.time(), "A", True,
        None, None, None, None)
    assert is_fast is True
    assert vc._wake_session.target == "A"


# ── v2：_wake_session_on_replied(winner) / _wake_session_on_shortcut / sticky grounded ──

from intent_bus import Bid


def _bid(name, *, missing_slots=None, reason=""):
    return Bid(name=name, confidence=0.9, handler=AsyncMock(),
               reason=reason, missing_slots=missing_slots or [])


def test_on_replied_music_winner_no_missing_slots_ends_session():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    with patch("time.time", return_value=1.0):
        cog._wake_session_on_replied(_bid("music", missing_slots=[]))
    assert cog._wake_session.target is None


@pytest.mark.asyncio
async def test_on_replied_music_winner_with_missing_slots_does_not_end_session():
    cog = _FakeCog()
    cog._wake_session_play_ack = AsyncMock()   # 照常 on_replied → 會排提示音 task
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    with patch("time.time", return_value=0.5):
        cog._wake_session_on_replied(_bid("music", missing_slots=["song_choice"]))
    assert cog._wake_session.target == "A"
    assert cog._wake_session.opened is True


def test_on_replied_replay_winner_ends_session():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    with patch("time.time", return_value=1.0):
        cog._wake_session_on_replied(_bid("replay"))
    assert cog._wake_session.target is None


def test_on_replied_playback_control_skip_reason_ends_session():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    with patch("time.time", return_value=1.0):
        cog._wake_session_on_replied(_bid("playback_control", reason="skip:下一首"))
    assert cog._wake_session.target is None


# 10/23 實測：「馬文音樂大聲一點」「法文繼續播放啊」做完窗沒關，接走朋友互聊 2 分鐘
# （每句播 thinking 提示音）。指令一律要喚醒詞 → 任何指令執行完都收窗。
@pytest.mark.parametrize("reason", ["pause:暫停", "resume:繼續播", "stop:停止播放"])
def test_on_replied_playback_control_any_reason_ends_session(reason):
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    with patch("time.time", return_value=1.0):
        cog._wake_session_on_replied(_bid("playback_control", reason=reason))
    assert cog._wake_session.target is None


def test_on_replied_volume_winner_ends_session():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    with patch("time.time", return_value=1.0):
        cog._wake_session_on_replied(_bid("volume"))
    assert cog._wake_session.target is None


def test_on_shortcut_play_prefix_ends_session():
    from music_fastpath import FASTPATH_PLAY_PREFIX
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    with patch("time.time", return_value=1.0):
        cog._wake_session_on_shortcut(FASTPATH_PLAY_PREFIX + "晴天")
    assert cog._wake_session.target is None


def test_on_shortcut_skip_ends_session():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    with patch("time.time", return_value=1.0):
        cog._wake_session_on_shortcut("下一首")
    assert cog._wake_session.target is None


@pytest.mark.parametrize("sc", ["暫停音樂", "繼續播", "大聲一點"])
def test_on_shortcut_any_command_ends_session(sc):
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session_play_ack = AsyncMock()
    with patch("time.time", return_value=0.5):
        cog._wake_session_on_shortcut(sc)
    assert cog._wake_session.target is None
    cog._wake_session_play_ack.assert_not_called()


def test_followup_override_closing_text_ends_session():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    with patch("time.time", return_value=1.0):
        assert cog._followup_override("A", None, "謝謝") is False
    assert cog._wake_session.target is None


def test_apply_wake_guards_closing_text_ends_listening_session():
    """真 VoiceController._apply_wake_guards：session 聆聽中對象說結束語 → raw_text 有接上。"""
    from tests.test_wake_guards_unit import _make_vc
    session = WakeSession()
    session.on_wake_accepted("A", time.time())
    session.on_replied(time.time())
    vc = _make_vc(_wake_session=session, _mixer=None)
    is_fast, _, _ = vc._apply_wake_guards(
        "A", "謝謝", time.time(), "A", False,
        None, None, None, None)
    assert is_fast is False
    assert vc._wake_session.target is None


# ── voice_controller.py 接線：winner 有傳進 on_replied / shortcut 有走 on_shortcut ──

def _listening_session(speaker):
    s = WakeSession()
    t0 = time.time()
    s.on_wake_accepted(speaker, t0)
    s.on_replied(t0)
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize("winner_name,ended", [("replay", True), ("volume", True), ("grounded_qa", False)])
async def test_process_queued_query_passes_winner_to_session(winner_name, ended):
    from intent_bus import Bid
    from tests.test_process_queued_query_wav_capture import _make_cog as _make_pq_cog
    cog = _make_pq_cog()
    cog._wake_session = _listening_session("Alice")
    cog._wake_session_play_ack = AsyncMock()
    cog._intent_bus.dispatch = AsyncMock(return_value=Bid(name=winner_name, confidence=0.9, handler=AsyncMock()))
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value="再放一次")

    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)

    assert (cog._wake_session.target is None) is ended


@pytest.mark.asyncio
async def test_handle_stt_result_shortcut_skip_ends_session():
    """session 接住「馬文下一首」（有叫名字）→ WakeShortcut 直派 → 走 _wake_session_on_shortcut → 結束。"""
    import wake_shortcut
    cog, wf = _make_cog_for_stt()
    wf.is_open.return_value = False
    cog._wake_session = _listening_session("TestUser")
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    with patch.object(wake_shortcut, "shortcut_query", return_value="下一首"), \
         patch.object(wake_shortcut, "dispatch_with_feedback", AsyncMock()):
        await cog.handle_stt_result("TestUser", "馬文下一首", time.time(), b"fake_audio", bypass_etd=True)

    assert cog._wake_session.target is None
    assert "TestUser" not in cog.speech_buffers


@pytest.mark.asyncio
async def test_handle_stt_result_shortcut_clears_speech_buffers():
    """WakeShortcut 直派時必須清掉 speech_buffers[speaker]，不讓歌名/指令殘留。"""
    import wake_shortcut
    cog, wf = _make_cog_for_stt()
    wf.is_open.return_value = False
    wf.multi_channel_decide.return_value = (
        True, 0.95,
        {"voice": 0.95, "task": 0.0, "info": 0.0, "control": 0.0,
         "threshold": 0.35, "total": 0.95},
    )
    cog._wake_session = None  # 無 session（首次喚醒）
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    with patch.object(wake_shortcut, "shortcut_query", return_value="下一首"), \
         patch.object(wake_shortcut, "dispatch_with_feedback", AsyncMock()):
        await cog.handle_stt_result("TestUser", "馬文下一首", time.time(), b"fake_audio", bypass_etd=True)

    assert "TestUser" not in cog.speech_buffers


@pytest.mark.asyncio
async def test_handle_stt_result_wakeless_shortcut_in_window_not_dispatched():
    """10/24 使用者定：窗內沒叫名字的「下一首」可能只是在討論 → WakeShortcut 不直派。"""
    import wake_shortcut
    cog, wf = _make_cog_for_stt()
    wf.is_open.return_value = False
    cog._wake_session = _listening_session("TestUser")
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    with patch.object(wake_shortcut, "shortcut_query", return_value="下一首"), \
         patch.object(wake_shortcut, "dispatch_with_feedback", AsyncMock()) as dispatched:
        await cog.handle_stt_result("TestUser", "下一首", time.time(), b"fake_audio", bypass_etd=True)

    dispatched.assert_not_called()
    assert cog._wake_session.target == "TestUser"


# ── v3：提示音等「Marvin 開始講 → 講完」才響 ─────────────────────────────────

def _seq_then_zero(seq):
    """回傳一個 side_effect 函式：按序列回值，耗盡後持續回 0.0。"""
    it = iter(seq)

    def _side_effect():
        try:
            return next(it)
        except StopIteration:
            return 0.0
    return _side_effect


@pytest.mark.asyncio
async def test_play_ack_waits_for_marvin_to_start_speaking_before_playing(monkeypatch):
    import cogs.voice_controller_wake_session as wsm
    monkeypatch.setattr(wsm, "_ACK_POLL_S", 0.001)
    monkeypatch.setattr(wsm, "_ACK_IDLE_REQUIRED_S", 0.002)
    monkeypatch.setattr(wsm, "_ACK_SPEECH_WAIT_S", 1.0)
    monkeypatch.setattr(wsm, "_ACK_MAX_WAIT_S", 1.0)

    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.side_effect = _seq_then_zero([0.0, 0.0, 0.0, 1.0, 1.0, 1.0])
    cog.play_dj_on_tts_layer = AsyncMock(return_value=True)

    await cog._wake_session_play_ack("A")

    cog.play_dj_on_tts_layer.assert_called_once_with(WAKE_SESSION_ACK_PATH, peak=0.15)
    assert cog._mixer.tts_load_seconds.call_count > 6, "提示音不該在 Marvin 開口前就播"


# ── C：_wake_session_captured（連續對話接住的句子不疊 filler ack）───────────

def test_wake_session_captured_true_when_target_opened():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    assert cog._wake_session_captured("A") is True


def test_wake_session_captured_false_for_other_speaker():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    assert cog._wake_session_captured("B") is False


def test_wake_session_captured_false_when_not_opened_yet():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    assert cog._wake_session_captured("A") is False


def test_wake_session_captured_false_when_no_session():
    cog = _FakeCog()
    cog._wake_session = None
    assert cog._wake_session_captured("A") is False


# ── D/E：_reply_address_prefix / _speak_reply_sentence ───────────────────────

class _FakeSpeakCog(WakeSessionMixin):
    def __init__(self, *, stream_mode, online_members):
        self._wake_session = None
        self._mixer = None
        self._wake_response_pending = False
        self.stream_mode = stream_mode
        self._online_members = online_members
        self.play_tts = AsyncMock()

    def get_online_members(self):
        return self._online_members


@pytest.mark.asyncio
async def test_speak_reply_sentence_stream_mode_skips_play_tts():
    cog = _FakeSpeakCog(stream_mode=True, online_members=["showay", "B"])
    cog._speak_reply_sentence("showay", "句子", first=True)
    await asyncio.sleep(0)
    cog.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_speak_reply_sentence_multi_member_first_adds_name():
    cog = _FakeSpeakCog(stream_mode=False, online_members=["showay", "B"])
    cog._speak_reply_sentence("showay", "句子", first=True)
    await asyncio.sleep(0)
    cog.play_tts.assert_called_once_with("showay，句子", already_in_channel=True, emotion_tag="neutral")


@pytest.mark.asyncio
async def test_speak_reply_sentence_not_first_no_name():
    cog = _FakeSpeakCog(stream_mode=False, online_members=["showay", "B"])
    cog._speak_reply_sentence("showay", "句子", first=False)
    await asyncio.sleep(0)
    cog.play_tts.assert_called_once_with("句子", already_in_channel=True, emotion_tag="neutral")


@pytest.mark.asyncio
async def test_speak_reply_sentence_single_member_no_name():
    cog = _FakeSpeakCog(stream_mode=False, online_members=["showay"])
    cog._speak_reply_sentence("showay", "句子", first=True)
    await asyncio.sleep(0)
    cog.play_tts.assert_called_once_with("句子", already_in_channel=True, emotion_tag="neutral")


def test_reply_address_prefix_exception_returns_empty():
    cog = _FakeSpeakCog(stream_mode=False, online_members=[])
    cog.get_online_members = MagicMock(side_effect=Exception("boom"))
    assert cog._reply_address_prefix("showay") == ""


# ── D 接線守門：_stream_response 不准再直呼 play_tts(sentence ───────────────

def test_stream_response_source_uses_speak_reply_sentence_not_play_tts():
    import inspect
    from cogs.voice_controller import VoiceController
    src = inspect.getsource(VoiceController._stream_response)
    assert "self._speak_reply_sentence(" in src
    assert "self.play_tts(sentence" not in src


@pytest.mark.asyncio
async def test_play_ack_speech_timeout_still_plays(monkeypatch):
    """沒有語音回應（一路靜默）→ 逾時後仍進入等空閒並播出，不會卡死。"""
    import cogs.voice_controller_wake_session as wsm
    monkeypatch.setattr(wsm, "_ACK_POLL_S", 0.001)
    monkeypatch.setattr(wsm, "_ACK_IDLE_REQUIRED_S", 0.002)
    monkeypatch.setattr(wsm, "_ACK_SPEECH_WAIT_S", 0.005)
    monkeypatch.setattr(wsm, "_ACK_MAX_WAIT_S", 1.0)

    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog.play_dj_on_tts_layer = AsyncMock(return_value=True)

    await cog._wake_session_play_ack("A")

    cog.play_dj_on_tts_layer.assert_called_once_with(WAKE_SESSION_ACK_PATH, peak=0.15)
def test_process_query_task_filler_gated_by_wake_session_captured():
    """C 接線守門：連續對話接住的句子不疊 filler ack（10/17）。"""
    import inspect
    from cogs.voice_controller import VoiceController
    src = inspect.getsource(VoiceController._process_query_task)
    assert "if not self._wake_session_captured(speaker):" in src


def test_stream_response_mid_song_speak_has_address_prefix():
    """E 接線守門：放歌中整段 speak 也要叫名字（逐句已被 D 跳過，這是唯一發聲點）。"""
    import inspect
    from cogs.voice_controller import VoiceController
    src = inspect.getsource(VoiceController._stream_response)
    assert "self._speak_whole_reply(" in src
    src = inspect.getsource(VoiceController._speak_whole_reply)
    assert "self.speak(self._reply_address_prefix(speaker, full_text) + full_text" in src


# ── in_conversation_window：唯讀接點，VAD（voice_recv）執行緒也會呼叫 ──────────

def test_in_conversation_window_without_session_is_false():
    cog = _FakeCog()
    cog._wake_session = None
    assert cog.in_conversation_window("A", 1.0) is False


def test_in_conversation_window_delegates_to_session():
    from wake_session import WakeSession
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(1.0)
    assert cog.in_conversation_window("A", 2.0) is True
    assert cog.in_conversation_window("B", 2.0) is False


def test_in_conversation_window_callable_from_non_loop_thread():
    import threading
    from wake_session import WakeSession
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(1.0)
    before = dict(vars(cog._wake_session))
    out = {}

    def _worker():
        try:
            out["v"] = cog.in_conversation_window("A", 2.0)
        except BaseException as e:   # noqa: BLE001 — 要把任何例外帶回主執行緒斷言
            out["err"] = e

    t = threading.Thread(target=_worker)
    t.start()
    t.join(5)
    assert "err" not in out
    assert out["v"] is True
    assert vars(cog._wake_session) == before
