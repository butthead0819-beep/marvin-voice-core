"""
session barge-in 接線測試（10/22）：

- handle_raw_speech_start：只建 watcher task，不同步 clear_tts（真正的切交給 _barge_in_watch）
- _barge_in_watch：confirm_step 推進到 cut → 切 TTS + 等講完播 ack；dip → 不切
- _followup_override：barge_captured → 排程 _barge_in_confirm；is_wake_check 傳 final=False
- _barge_in_confirm：router 回有效句才念，主答案已開始或回「略」都不念
- _stream_tts_to_mixer：pushed==1 時記進 _reply_lines
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.voice_controller import VoiceController
from cogs.voice_controller_wake_session import WakeSessionMixin
from wake_session import WakeSession


# ── handle_raw_speech_start：只建 watcher，不同步切 ───────────────────────────

def _make_barge_fake(*, target="Alice", opened=True, speaking=True,
                      tts_protected=False, shield_until=0.0, no_session=False):
    fake = MagicMock()
    if no_session:
        fake._wake_session = None
    else:
        session = MagicMock()
        session.target = target
        session.opened = opened
        fake._wake_session = session
    fake._marvin_tts_speaking = MagicMock(return_value=speaking)
    fake._tts_protected = tts_protected
    fake._tts_barge_shield_until = shield_until
    fake._barge_watch_task = None
    fake.user_states = {}
    fake.last_player_speech_time = 0.0
    fake._mixer = MagicMock()
    fake.is_playing_audio = False
    fake.last_marvin_speech_time = 0.0
    # 讓後段「延遲嘲諷」早退，不污染斷言（照 test_tts_interrupt_device_path.py 的手法）
    fake.bot.voice_clients = []
    fake.bot.engine.conv_buffer.get_conversation_temperature.return_value = 2.0
    return fake


@pytest.mark.asyncio
async def test_non_target_speaker_does_not_create_watcher():
    fake = _make_barge_fake(target="Bob")
    WakeSessionMixin._maybe_start_barge_watch(fake, "Alice", 1)
    await asyncio.sleep(0)
    fake._mixer.clear_tts.assert_not_called()
    assert fake._barge_watch_task is None


@pytest.mark.asyncio
async def test_no_session_does_not_create_watcher():
    fake = _make_barge_fake(no_session=True)
    WakeSessionMixin._maybe_start_barge_watch(fake, "Alice", 1)
    await asyncio.sleep(0)
    fake._mixer.clear_tts.assert_not_called()
    assert fake._barge_watch_task is None


@pytest.mark.asyncio
async def test_tts_protected_does_not_create_watcher():
    fake = _make_barge_fake(target="Alice", tts_protected=True)
    WakeSessionMixin._maybe_start_barge_watch(fake, "Alice", 1)
    await asyncio.sleep(0)
    assert fake._barge_watch_task is None


@pytest.mark.asyncio
async def test_shielded_does_not_create_watcher():
    fake = _make_barge_fake(target="Alice", shield_until=time.time() + 10.0)
    WakeSessionMixin._maybe_start_barge_watch(fake, "Alice", 1)
    await asyncio.sleep(0)
    assert fake._barge_watch_task is None


@pytest.mark.asyncio
async def test_not_speaking_does_not_create_watcher():
    fake = _make_barge_fake(target="Alice", speaking=False)
    WakeSessionMixin._maybe_start_barge_watch(fake, "Alice", 1)
    await asyncio.sleep(0)
    assert fake._barge_watch_task is None


@pytest.mark.asyncio
async def test_target_opened_speaking_creates_watcher():
    fake = _make_barge_fake(target="Alice", opened=True, speaking=True)
    fake._barge_in_watch = AsyncMock()
    WakeSessionMixin._maybe_start_barge_watch(fake, "Alice", 42)
    await asyncio.sleep(0)
    assert fake._barge_watch_task is not None
    fake._barge_in_watch.assert_called_once()
    call_args = fake._barge_in_watch.call_args[0]
    assert call_args[0] == "Alice"
    assert call_args[1] == 42
    assert isinstance(call_args[2], float)


@pytest.mark.asyncio
async def test_existing_unfinished_watcher_not_replaced():
    fake = _make_barge_fake(target="Alice")
    fake._barge_in_watch = AsyncMock()
    old_task = asyncio.get_event_loop().create_future()
    fake._barge_watch_task = old_task
    WakeSessionMixin._maybe_start_barge_watch(fake, "Alice", 42)
    await asyncio.sleep(0)
    assert fake._barge_watch_task is old_task
    fake._barge_in_watch.assert_not_called()
    old_task.cancel()


def test_handle_raw_speech_start_delegates_to_barge_gate():
    """VAD 回呼只委派給 _maybe_start_barge_watch，自己不碰 mixer TTS 層。"""
    fake = _make_barge_fake(target="Alice")
    VoiceController.handle_raw_speech_start(fake, "Alice", user_id=42)
    fake._maybe_start_barge_watch.assert_called_once_with("Alice", 42)
    fake._mixer.clear_tts.assert_not_called()


# ── _speak_reply_sentence 世代號：被打斷的舊串流後續句子不念 ─────────────────

class _SpeakFakeCog(WakeSessionMixin):
    def __init__(self):
        self._init_barge_in_state()
        self.stream_mode = False
        self.play_tts = AsyncMock()

    def _reply_address_prefix(self, speaker, text=""):
        return ""


@pytest.mark.asyncio
async def test_old_stream_sentences_dropped_after_barge_in():
    cog = _SpeakFakeCog()

    async def old_stream():
        cog._speak_reply_sentence("A", "第一句", first=True)
        await asyncio.sleep(0)
        cog._reply_gen += 1          # watcher 切掉了
        await new_turn               # 新一輪先開始念
        cog._speak_reply_sentence("A", "舊的第二句", first=False)

    async def new_stream():
        cog._speak_reply_sentence("A", "新答案", first=True)
        cog._speak_reply_sentence("A", "新答案第二句", first=False)

    new_turn = asyncio.ensure_future(asyncio.sleep(0))
    t_old = asyncio.create_task(old_stream())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.create_task(new_stream())
    await t_old
    await asyncio.sleep(0)
    said = [c.args[0] for c in cog.play_tts.call_args_list]
    assert said == ["第一句", "新答案", "新答案第二句"]


@pytest.mark.asyncio
async def test_same_stream_keeps_speaking_without_barge_in():
    cog = _SpeakFakeCog()

    async def stream():
        cog._speak_reply_sentence("A", "一", first=True)
        cog._speak_reply_sentence("A", "二", first=False)

    await asyncio.create_task(stream())
    await asyncio.sleep(0)
    assert [c.args[0] for c in cog.play_tts.call_args_list] == ["一", "二"]


# ── _barge_in_watch：cut → 切 + 等講完播 ack；dip → 不切 ─────────────────────

def _make_watch_fake(*, target="Alice", opened=True):
    fake = MagicMock()
    sink = MagicMock()
    sink.user_last_spoken_time = {}
    fake.bot.engine.get_active_sink.return_value = sink
    session = MagicMock()
    session.target = target
    session.opened = opened
    fake._wake_session = session
    fake._marvin_tts_speaking = MagicMock(return_value=True)
    fake._plan12 = True
    fake._mixer = MagicMock()
    fake._reply_lines = [(0.0, "未說完的話")]
    fake._wake_session_ack_task = None
    fake._tts_interrupted = False
    fake._reply_gen = 0
    fake.stt_logger = MagicMock()
    fake.play_dj_on_tts_layer = AsyncMock(return_value=True)
    return fake, sink


@pytest.mark.asyncio
async def test_barge_in_watch_cuts_then_plays_ack(monkeypatch):
    import cogs.voice_controller_wake_session as wsm
    clock = {"t": 0.0}
    monkeypatch.setattr(wsm.time, "time", lambda: clock["t"])

    async def _advance_sleep(s):
        clock["t"] += s
        if clock["t"] < 0.8 - 1e-9:
            sink.user_last_spoken_time[7] = clock["t"]
    monkeypatch.setattr(wsm.asyncio, "sleep", _advance_sleep)

    fake, sink = _make_watch_fake()
    sink.user_last_spoken_time[7] = 0.0
    chime = MagicMock()
    chime.done.return_value = False
    fake._wake_session_ack_task = chime   # 開窗提示音還在等 → 切的時候要取消

    await WakeSessionMixin._barge_in_watch(fake, "Alice", 7, 0.0)

    chime.cancel.assert_called_once()
    fake._mixer.clear_tts.assert_called_once()
    assert fake._tts_interrupted is True
    assert fake._reply_gen == 1
    fake._wake_session.note_barge_in.assert_called_once()
    fake._wake_session.mark_last_answer_interrupted.assert_called_once()
    fake.play_dj_on_tts_layer.assert_called_once()
    call_args = fake.play_dj_on_tts_layer.call_args[0]
    from barge_in import BARGE_ACK_FILES
    assert call_args[0] in BARGE_ACK_FILES


@pytest.mark.asyncio
async def test_barge_in_watch_dip_does_not_cut(monkeypatch):
    import cogs.voice_controller_wake_session as wsm
    clock = {"t": 0.0}
    monkeypatch.setattr(wsm.time, "time", lambda: clock["t"])

    async def _advance_sleep(s):
        clock["t"] += s
        if clock["t"] < 0.3 - 1e-9:
            sink.user_last_spoken_time[7] = clock["t"]
    monkeypatch.setattr(wsm.asyncio, "sleep", _advance_sleep)

    fake, sink = _make_watch_fake()
    sink.user_last_spoken_time[7] = 0.0

    await WakeSessionMixin._barge_in_watch(fake, "Alice", 7, 0.0)

    fake._mixer.clear_tts.assert_not_called()
    fake.play_dj_on_tts_layer.assert_not_called()


@pytest.mark.asyncio
async def test_barge_in_watch_returns_when_no_sink():
    fake, sink = _make_watch_fake()
    fake.bot.engine.get_active_sink.return_value = None
    await WakeSessionMixin._barge_in_watch(fake, "Alice", 7, time.time())
    fake._mixer.clear_tts.assert_not_called()


@pytest.mark.asyncio
async def test_barge_in_watch_returns_when_marvin_stopped_speaking():
    fake, sink = _make_watch_fake()
    fake._marvin_tts_speaking = MagicMock(return_value=False)
    await WakeSessionMixin._barge_in_watch(fake, "Alice", 7, time.time())
    fake._mixer.clear_tts.assert_not_called()


# ── _followup_override：barge 接住 → 排 _barge_in_confirm；is_wake_check → final=False ──

class _FakeCog(WakeSessionMixin):
    def __init__(self):
        self._wake_session = None
        self._mixer = None
        self._wake_response_pending = False


@pytest.mark.asyncio
async def test_followup_override_schedules_barge_in_confirm_when_captured():
    cog = _FakeCog()
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted("A", 0.0)
    cog._wake_session.on_replied(0.5)
    cog._wake_session.note_barge_in(1.0)
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 1.0   # Marvin 還在講（ack）
    cog._barge_in_confirm = AsyncMock()

    with patch("time.time", return_value=1.5):
        assert cog._followup_override("A", None, "新問題") is True
    await asyncio.sleep(0)

    cog._barge_in_confirm.assert_called_once_with("A", "新問題")
    assert cog._wake_session.barge_captured is False


@pytest.mark.asyncio
async def test_followup_override_passes_final_false_for_wake_check():
    cog = _FakeCog()
    cog._wake_session = MagicMock()
    cog._wake_session.on_utterance.return_value = False
    cog._wake_session.barge_captured = False
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0

    cog._followup_override("A", None, "半句", is_wake_check=True)

    _, kwargs = cog._wake_session.on_utterance.call_args
    assert kwargs.get("final") is False


@pytest.mark.asyncio
async def test_followup_override_default_final_true():
    cog = _FakeCog()
    cog._wake_session = MagicMock()
    cog._wake_session.on_utterance.return_value = False
    cog._wake_session.barge_captured = False
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0

    cog._followup_override("A", None, "整句")

    _, kwargs = cog._wake_session.on_utterance.call_args
    assert kwargs.get("final") is True


# ── _barge_in_confirm ─────────────────────────────────────────────────────────

class _ConfirmFakeCog(WakeSessionMixin):
    def __init__(self):
        self._reply_lines = []
        self._shared_tier_router = None
        self._tts_interrupted = True
        self._stream_tts_to_mixer = AsyncMock()


@pytest.mark.asyncio
async def test_barge_in_confirm_speaks_valid_sentence():
    cog = _ConfirmFakeCog()
    cog._reply_lines = [(90.0, "之前的回答")]
    router = MagicMock()
    router.quick = AsyncMock(return_value="喔你是要問A。")
    cog._shared_tier_router = router

    with patch("time.time", return_value=100.0):
        await cog._barge_in_confirm("Alice", "其實我想問A")

    cog._stream_tts_to_mixer.assert_called_once()
    assert cog._stream_tts_to_mixer.call_args[0][0] == "喔你是要問A。"
    assert cog._tts_interrupted is False


@pytest.mark.asyncio
async def test_barge_in_confirm_skips_when_main_answer_already_started():
    cog = _ConfirmFakeCog()
    router = MagicMock()
    router.quick = AsyncMock(return_value="喔你是要問A。")
    cog._shared_tier_router = router

    with patch("time.time", return_value=100.0):
        cog._reply_lines = [(150.0, "主答案已經開始念了")]
        await cog._barge_in_confirm("Alice", "其實我想問A")

    cog._stream_tts_to_mixer.assert_not_called()


@pytest.mark.asyncio
async def test_barge_in_confirm_skips_when_router_returns_lue():
    cog = _ConfirmFakeCog()
    router = MagicMock()
    router.quick = AsyncMock(return_value="略")
    cog._shared_tier_router = router

    await cog._barge_in_confirm("Alice", "什麼都看不出")

    cog._stream_tts_to_mixer.assert_not_called()


@pytest.mark.asyncio
async def test_barge_in_confirm_skips_when_no_router():
    cog = _ConfirmFakeCog()
    cog._shared_tier_router = None

    await cog._barge_in_confirm("Alice", "其實我想問A")

    cog._stream_tts_to_mixer.assert_not_called()


# ── _stream_tts_to_mixer 記錄 _reply_lines ───────────────────────────────────

from tests.test_playback_tts_path import _make_cog_real_stream_tts, _fake_ffmpeg_proc_one_frame


@pytest.mark.asyncio
async def test_stream_tts_to_mixer_appends_reply_lines_on_first_frame():
    cog = _make_cog_real_stream_tts()
    assert hasattr(cog, "_reply_lines")

    async def _gen(*args, **kwargs):
        return
        yield  # noqa: unreachable
    cog.bot.tts_engine.stream_audio = MagicMock(side_effect=_gen)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_fake_ffmpeg_proc_one_frame())):
        pushed = await cog._stream_tts_to_mixer(
            "測試句子", force_macos=False, emotion_tag="neutral", voice=None
        )

    assert pushed == 1
    assert len(cog._reply_lines) == 1
    assert cog._reply_lines[-1][1] == "測試句子"
