"""PuckMicSession 全時收音狀態機 — 用假 VAD / 假 Whisper model 驗證狀態機，
不載真 ONNX / 真 Whisper（跟 tests/test_puck_vad_stream.py 同手法）。

WakeStreamDetector 參數（window_s=1.0, stride_s=0.25, input_rate=16000,
input_channels=1）換算成 bytes 門檻：window_bytes=32000, stride_bytes=8000。
PuckVADConfig 預設 min_speech_windows=8、end_silence_windows=22——這些數字
決定下面測試要餵多少個 512-sample window 才能觸發第一次 Whisper 推理 /
VAD 切句，詳見各測試內註解。
"""
from __future__ import annotations

import asyncio
import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from ima_adpcm import BLOCK_SAMPLES, ImaAdpcmEncoder
from puck_mic_session import PuckMicConfig, PuckMicSession


class FakeVAD:
    """process() 吃任意長度 PCM，依 512-sample window 切，回傳預排機率；
    排好的機率用完後回 0.0（安全預設：不會誤觸發任何狀態轉移）。"""

    def __init__(self, probs):
        self._probs = list(probs)
        self._idx = 0

    def process(self, pcm16: np.ndarray) -> list[float]:
        n_windows = len(pcm16) // 512
        out = []
        for _ in range(n_windows):
            out.append(self._probs[self._idx] if self._idx < len(self._probs) else 0.0)
            self._idx += 1
        return out


class FakeWhisperModel:
    """transcribe() 依呼叫次數回預排文字；忽略音訊內容本身。"""

    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = 0

    def transcribe(self, audio, **kwargs):
        text = self._texts[self.calls] if self.calls < len(self._texts) else ""
        self.calls += 1
        return [SimpleNamespace(text=text)], None


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _block(value: int = 1000) -> np.ndarray:
    return np.full(BLOCK_SAMPLES, value, dtype=np.int16)


async def _feed_n(session: PuckMicSession, encoder: ImaAdpcmEncoder, n: int, value: int = 1000) -> None:
    data = b"".join(encoder.encode_block(_block(value)) for _ in range(n))
    await session.feed_adpcm(data)


async def _flush() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _face_states(send_json: AsyncMock) -> list[str]:
    return [c.args[0]["state"] for c in send_json.await_args_list
            if c.args[0].get("type") == "face"]


def _make_session(*, wake_texts, confirm_stt=None, deliver=None, send_json=None,
                   tts_frames_total=None, clock=None, config=None):
    loop = asyncio.get_event_loop()
    model = FakeWhisperModel(wake_texts)
    session = PuckMicSession(
        device="test_dev", speaker="狗與露", whisper_model=model, loop=loop,
        send_json=send_json or AsyncMock(),
        confirm_stt=confirm_stt or AsyncMock(return_value=""),
        deliver=deliver or AsyncMock(),
        tts_frames_total=tts_frames_total or (lambda: 0),
        clock=clock or (lambda: 0.0),
        vad=FakeVAD([0.9] * 8 + [0.9] * 24 + [0.1] * 22),
        config=config or PuckMicConfig(),
        wake_engine="whisper",
    )
    return session, model


# ── 1. 候選喚醒 → duck + capture；VAD 切句後 confirm_stt 收到正確 WAV ──────────
@pytest.mark.asyncio
async def test_candidate_wake_ducks_and_captures_wav():
    send_json = AsyncMock()
    confirm_stt = AsyncMock(return_value="")
    session, model = _make_session(wake_texts=["馬文下一首"], send_json=send_json, confirm_stt=confirm_stt)
    encoder = ImaAdpcmEncoder()

    # 8 候選 + 24 延續 = 32 個高機率 window 後第一次 Whisper 推理觸發（見檔頭說明）。
    await _feed_n(session, encoder, 32, value=1000)
    await _flush()

    assert session.state == "capture"
    send_json.assert_any_await({"type": "duck"})
    assert session.stats["candidates"] == 1

    # 22 個低機率 window → VAD 切句結束，state 轉 confirm，confirm_stt 被呼叫。
    await _feed_n(session, encoder, 22, value=1000)
    await _flush()

    confirm_stt.assert_awaited_once()
    wav_bytes = confirm_stt.call_args.args[0]
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2
        n_frames = wf.getnframes()
    assert n_frames >= PuckMicConfig().capture_pre_s * 16000


# ── 2. confirm_stt 命中喚醒詞 → deliver 收到剝除喚醒詞的指令、state=reply ─────
@pytest.mark.asyncio
async def test_confirm_hit_delivers_stripped_command():
    deliver = AsyncMock()
    confirm_stt = AsyncMock(return_value="黑馬威下一首")
    session, model = _make_session(
        wake_texts=["馬文下一首"], confirm_stt=confirm_stt, deliver=deliver)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    await _feed_n(session, encoder, 22, value=1000)
    await _flush()

    deliver.assert_awaited_once_with("狗與露", "馬文，下一首")
    assert session.state == "reply"
    assert session.stats["confirmed"] == 1
    assert session.stats["rejected"] == 0


# ── 3. confirm_stt 否決 → unduck、回 listen、rejected++、deliver 未呼叫；
#      同一段語音（VAD 未結束）rearm 後能再出候選 ──────────────────────────
@pytest.mark.asyncio
async def test_confirm_reject_unducks_and_rearms_same_segment():
    send_json = AsyncMock()
    deliver = AsyncMock()
    confirm_stt = AsyncMock(return_value="大家好")
    # capture_max_s 設很短：候選喚醒後很快就用 max 收尾，VAD 還在 SPEAKING
    # 中途（還沒餵 22 個低機率 window），buffer 沒被 on_speech_end 清掉，
    # 才能驗證「同一段語音 rearm 後再出候選」。
    config = PuckMicConfig(capture_max_s=0.1)
    session, model = _make_session(
        wake_texts=["馬文下一首", "馬文下一首"], send_json=send_json, confirm_stt=confirm_stt,
        deliver=deliver, config=config)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    assert session.state == "capture"

    # capture_max_s=0.1s → 0.1*16000=1600 samples ≈ 4 個 block 後觸發 max 收尾。
    await _feed_n(session, encoder, 4, value=1000)
    await _flush()

    assert session.stats["rejected"] == 1
    deliver.assert_not_awaited()
    assert session.state == "listen"
    send_json.assert_any_await({"type": "unduck"})

    # rearm 後，VAD 同一段語音繼續餵高機率 window → 能再出第二次候選。
    await _feed_n(session, encoder, 8, value=1000)
    await _flush()

    assert session.stats["candidates"] == 2
    assert session.state == "capture"


# ── 4. capture_max_s 到（VAD 一直 speech 不停）→ 照樣結束收音進 confirm ──────
@pytest.mark.asyncio
async def test_capture_max_deadline_finishes_even_without_vad_silence():
    confirm_stt = AsyncMock(return_value="")
    config = PuckMicConfig(capture_max_s=0.1)
    session, model = _make_session(wake_texts=["馬文下一首"], confirm_stt=confirm_stt, config=config)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    assert session.state == "capture"

    await _feed_n(session, encoder, 4, value=1000)
    await _flush()

    confirm_stt.assert_awaited_once()


# ── 5. reply 階段：Marvin 持續講話 hold_s 內、停止後安靜超過 idle_s → unduck ──
# 緊貼兩個門檻（speak_hold_s 與 unduck_idle_s）分別檢查，而非一次跳過兩者總和：
# 確保 mutation 任一常數都會讓這個測試翻紅（而不是因為餘裕夠大而繼續通過）。
@pytest.mark.asyncio
async def test_reply_unducks_after_speak_then_idle(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="puck_mic_session")

    send_json = AsyncMock()
    clock = FakeClock()
    frames = {"v": 0}
    config = PuckMicConfig()
    confirm_stt = AsyncMock(return_value="黑馬威下一首")
    session, model = _make_session(
        wake_texts=["馬文下一首"], send_json=send_json, confirm_stt=confirm_stt,
        tts_frames_total=lambda: frames["v"], clock=clock, config=config)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    await _feed_n(session, encoder, 22, value=1000)
    await _flush()
    assert session.state == "reply"

    # Marvin 開始講話：frames_total 遞增，之後每隔 0.5s 餵一次，持續到快滿
    # speak_hold_s 前——驗證「持續講話中」這段時間 state 必須一直是 reply
    # （若 speak_hold_s 被 mutate 變小，下面迴圈中途就會提早翻 listen）。
    frames["v"] += 1
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert session.state == "reply"

    step = 0.5
    t = 0.0
    while t + step < config.speak_hold_s:
        clock.advance(step)
        t += step
        await session.feed_adpcm(encoder.encode_block(_block(0)))
        assert session.state == "reply"

    # 講話停止。緊貼 unduck_idle_s 門檻兩側檢查。
    clock.advance(config.unduck_idle_s - 0.1)
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert session.state == "reply"

    clock.advance(0.2)
    await session.feed_adpcm(encoder.encode_block(_block(0)))

    assert session.state == "listen"
    send_json.assert_any_await({"type": "unduck"})
    assert "unduck（reply_done）" in caplog.text


# ── 6. reply 階段 Marvin 都沒講話 → reply_start_timeout_s 到 → unduck(no_reply) ─
# 緊貼門檻兩側檢查（timeout 前仍是 reply，timeout 後才 listen），避免餘裕過大
# 掩蓋掉 mutate reply_start_timeout_s 的破壞。
@pytest.mark.asyncio
async def test_reply_unducks_on_no_reply_timeout(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="puck_mic_session")

    send_json = AsyncMock()
    clock = FakeClock()
    config = PuckMicConfig()
    confirm_stt = AsyncMock(return_value="黑馬威下一首")
    session, model = _make_session(
        wake_texts=["馬文下一首"], send_json=send_json, confirm_stt=confirm_stt,
        tts_frames_total=lambda: 0, clock=clock, config=config)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    await _feed_n(session, encoder, 22, value=1000)
    await _flush()
    assert session.state == "reply"

    clock.advance(config.reply_start_timeout_s - 0.1)
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert session.state == "reply"

    clock.advance(0.2)
    await session.feed_adpcm(encoder.encode_block(_block(0)))

    assert session.state == "listen"
    send_json.assert_any_await({"type": "unduck"})
    assert "unduck（no_reply）" in caplog.text


# ── 7. listen 狀態 Marvin 講話（TTS 外洩進麥克風）→ 抑制 VAD；停止後解除 ─────
@pytest.mark.asyncio
async def test_listen_suppresses_vad_while_speaking():
    clock = FakeClock()
    frames = {"v": 0}
    config = PuckMicConfig()
    session, model = _make_session(
        wake_texts=[], tts_frames_total=lambda: frames["v"], clock=clock, config=config)
    encoder = ImaAdpcmEncoder()

    calls = []
    orig = session._vad_stream.set_suppressed

    def _spy(v):
        calls.append(v)
        return orig(v)

    session._vad_stream.set_suppressed = _spy

    # SpeakTracker 第一次 update 只記基準值、回 False（見其 docstring），先打一次
    # 奠基準，才能讓後面的遞增被判定為「正在講話」。
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert calls == []

    frames["v"] += 1
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert calls == [True]

    clock.advance(config.speak_hold_s + 0.1)
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert calls == [True, False]


# ── 8. feed_adpcm 長度非 260 倍數 → 丟棄、不拋例外、samples 不增加 ───────────
@pytest.mark.asyncio
async def test_feed_adpcm_rejects_bad_length():
    from ima_adpcm import BLOCK_BYTES
    session, model = _make_session(wake_texts=[])
    # 長度要夠跨過至少一個完整 block（> BLOCK_BYTES）又不是其倍數，否則
    # len(data)//BLOCK_BYTES 本來就是 0，就算拿掉長度檢查也不會真的處理到
    # 任何 block，測不出少了這個檢查。
    await session.feed_adpcm(b"x" * (BLOCK_BYTES * 2 + 5))
    assert session.state == "listen"
    assert session._samples_fed == 0


# ── 9. close()：ducked 狀態送 unduck；confirm task 被 cancel 不拋例外 ────────
@pytest.mark.asyncio
async def test_close_sends_unduck_and_cancels_confirm_task():
    send_json = AsyncMock()

    async def _never_returns(wav_bytes):
        await asyncio.sleep(100)
        return ""

    config = PuckMicConfig(capture_max_s=0.1)
    session, model = _make_session(
        wake_texts=["馬文下一首"], send_json=send_json, confirm_stt=_never_returns, config=config)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    assert session.state == "capture"

    # 觸發 max 收尾 → 建立 confirm task（卡在 _never_returns 裡）。
    await _feed_n(session, encoder, 4, value=1000)
    await asyncio.sleep(0)
    assert session.state == "confirm"
    confirm_task = session._confirm_task
    assert confirm_task is not None and not confirm_task.done()

    await session.close()   # 不應拋例外

    assert confirm_task.cancelled()
    send_json.assert_any_await({"type": "unduck"})


# ── 10. 候選喚醒的推理文字不出現在任何 log（含 wake_stream_detector）；確認後的指令可以 ──
@pytest.mark.asyncio
async def test_candidate_text_not_logged_anywhere(caplog):
    import logging
    caplog.set_level(logging.INFO)

    secret_candidate_text = "馬威欸這是不該被記錄的候選內容"
    confirm_stt = AsyncMock(return_value="黑馬威下一首")
    session, model = _make_session(wake_texts=[secret_candidate_text], confirm_stt=confirm_stt)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    await _feed_n(session, encoder, 22, value=1000)
    await _flush()

    assert secret_candidate_text not in caplog.text
    assert "下一首" in caplog.text


# ── 11. 指令已送出（reply）時斷線：deliver 不被取消 ───────────────────────────
@pytest.mark.asyncio
async def test_close_during_reply_does_not_cancel_deliver():
    finished = asyncio.Event()
    started = asyncio.Event()

    async def _slow_deliver(speaker, text):
        started.set()
        await asyncio.sleep(0.05)
        finished.set()

    confirm_stt = AsyncMock(return_value="黑馬威下一首")
    session, model = _make_session(
        wake_texts=["馬文下一首"], confirm_stt=confirm_stt, deliver=_slow_deliver)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    await _feed_n(session, encoder, 22, value=1000)
    await asyncio.wait_for(started.wait(), 1.0)
    assert session.state == "reply"

    await session.close()
    await asyncio.wait_for(finished.wait(), 1.0)
    assert finished.is_set()


# ── PTT 強制喚醒（puck 實體鍵 / 車機暫停鍵 → /mic_ws 文字 {"type":"ptt","src":...}） ──

def _make_ptt_session(*, probs, confirm_stt, deliver=None, send_json=None):
    loop = asyncio.get_event_loop()
    session = PuckMicSession(
        device="test_dev", speaker="狗與露", whisper_model=FakeWhisperModel([]), loop=loop,
        send_json=send_json or AsyncMock(),
        confirm_stt=confirm_stt,
        deliver=deliver or AsyncMock(),
        tts_frames_total=lambda: 0,
        clock=lambda: 0.0,
        vad=FakeVAD(probs),
        config=PuckMicConfig(),
        wake_engine="whisper",
    )
    return session


@pytest.mark.asyncio
async def test_ptt_button_skips_wake_requirement():
    send_json, deliver = AsyncMock(), AsyncMock()
    session = _make_ptt_session(probs=[0.9] * 32 + [0.1] * 22,
                                confirm_stt=AsyncMock(return_value="下一首"),
                                deliver=deliver, send_json=send_json)
    encoder = ImaAdpcmEncoder()

    await session.force_wake("button")
    assert session.state == "capture"
    send_json.assert_any_await({"type": "duck"})

    await _feed_n(session, encoder, 32 + 22)
    await _flush()

    deliver.assert_awaited_once_with("狗與露", "馬文，下一首")
    assert session.stats["ptt"] == 1
    assert session.stats["candidates"] == 0


@pytest.mark.asyncio
async def test_ptt_pause_still_requires_wake_word():
    send_json, deliver = AsyncMock(), AsyncMock()
    session = _make_ptt_session(probs=[0.9] * 32 + [0.1] * 22,
                                confirm_stt=AsyncMock(return_value="下一首"),
                                deliver=deliver, send_json=send_json)
    encoder = ImaAdpcmEncoder()

    await session.force_wake("pause")
    await _feed_n(session, encoder, 32 + 22)
    await _flush()

    deliver.assert_not_awaited()
    send_json.assert_any_await({"type": "unduck"})
    assert session.state == "listen"


@pytest.mark.asyncio
async def test_ptt_button_empty_stt_rejected():
    deliver = AsyncMock()
    session = _make_ptt_session(probs=[0.9] * 32 + [0.1] * 22,
                                confirm_stt=AsyncMock(return_value="  "), deliver=deliver)
    encoder = ImaAdpcmEncoder()

    await session.force_wake("button")
    await _feed_n(session, encoder, 32 + 22)
    await _flush()

    deliver.assert_not_awaited()
    assert session.state == "listen"


@pytest.mark.asyncio
async def test_ptt_during_ongoing_segment_ignores_that_segments_end():
    confirm_stt = AsyncMock(return_value="下一首")
    # 先 10 個高機率 window（VAD 已進 speech），按 PTT，再 22 個低機率 → 那段結束
    session = _make_ptt_session(probs=[0.9] * 10 + [0.1] * 400, confirm_stt=confirm_stt)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 10)
    await session.force_wake("button")
    await _feed_n(session, encoder, 22)
    await _flush()
    assert session.state == "capture"      # 按下前就開始的段落，結尾不算指令結尾
    confirm_stt.assert_not_awaited()

    # 撐到 capture_max_s（6s = 188 個 block）才收尾
    await _feed_n(session, encoder, 190)
    await _flush()
    confirm_stt.assert_awaited_once()


@pytest.mark.asyncio
async def test_ptt_ignored_when_not_listening():
    session = _make_ptt_session(probs=[], confirm_stt=AsyncMock(return_value=""))
    await session.force_wake("button")
    assert session.state == "capture"
    await session.force_wake("button")
    assert session.stats["ptt"] == 1


def test_wake_engine_defaults_to_swift(monkeypatch):
    from puck_mic_session import _puck_wake_engine
    monkeypatch.delenv("MARVIN_PUCK_WAKE_ENGINE", raising=False)
    assert _puck_wake_engine() == "swift"
    monkeypatch.setenv("MARVIN_PUCK_WAKE_ENGINE", "whisper")
    assert _puck_wake_engine() == "whisper"
    monkeypatch.setenv("MARVIN_PUCK_WAKE_ENGINE", "bogus")
    assert _puck_wake_engine() == "swift"


# ── 臉部狀態推送（/mic_ws {"type":"face","state":...}）────────────────────────

# ── 12. 第一包音訊後送出 idle；之後沒狀態變化不重送 ──────────────────────────
@pytest.mark.asyncio
async def test_face_idle_on_first_block_no_resend():
    send_json = AsyncMock()
    session, model = _make_session(wake_texts=[], send_json=send_json)
    encoder = ImaAdpcmEncoder()

    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json) == ["idle"]

    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json) == ["idle"]


# ── 13. 候選喚醒→listen（在 duck 之後）；VAD 切句進 confirm→think；否決→idle ──
@pytest.mark.asyncio
async def test_face_tracks_listen_think_idle_on_reject():
    send_json = AsyncMock()
    confirm_stt = AsyncMock(return_value="大家好")
    session, model = _make_session(
        wake_texts=["馬文下一首"], send_json=send_json, confirm_stt=confirm_stt)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    assert session.state == "capture"

    types = [c.args[0] for c in send_json.await_args_list]
    duck_idx = types.index({"type": "duck"})
    listen_idx = next(i for i, t in enumerate(types)
                       if t == {"type": "face", "state": "listen"})
    assert duck_idx < listen_idx

    await _feed_n(session, encoder, 22, value=1000)
    await _flush()

    assert session.state == "listen"
    assert _face_states(send_json) == ["idle", "listen", "think", "idle"]


# ── 14. confirm 命中進 reply→think；tts 增加 face_speak_delay_s 前仍 think、
#       到達後變 speak；講話停止+hold 過後+unduck_idle_s 到→reply_done→idle ──
@pytest.mark.asyncio
async def test_face_reply_delays_speak_then_idle_after_unduck():
    send_json = AsyncMock()
    clock = FakeClock()
    frames = {"v": 0}
    config = PuckMicConfig()
    confirm_stt = AsyncMock(return_value="黑馬威下一首")
    session, model = _make_session(
        wake_texts=["馬文下一首"], send_json=send_json, confirm_stt=confirm_stt,
        tts_frames_total=lambda: frames["v"], clock=clock, config=config)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    await _feed_n(session, encoder, 22, value=1000)
    await _flush()
    assert session.state == "reply"
    assert _face_states(send_json)[-1] == "think"

    # 講話開始（clock=0）：speaking=True，但 face_speak_delay_s 還沒到 → think
    frames["v"] += 1
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json)[-1] == "think"

    clock.advance(config.face_speak_delay_s - 0.1)
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json)[-1] == "think"

    clock.advance(0.2)  # 跨過 face_speak_delay_s 門檻
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json)[-1] == "speak"

    # 講話停止夠久（同時跨過 speak_hold_s 與 unduck_idle_s）→ reply_done → idle
    clock.advance(max(config.speak_hold_s, config.unduck_idle_s) + 0.1)
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert session.state == "listen"
    assert _face_states(send_json)[-1] == "idle"


# ── 15. listen 狀態下 tts 增加（DJ 口白等非喚醒回合）→ 延遲後 speak、停止後回 idle ──
@pytest.mark.asyncio
async def test_face_listen_state_speaks_during_narration():
    send_json = AsyncMock()
    clock = FakeClock()
    frames = {"v": 0}
    config = PuckMicConfig()
    session, model = _make_session(
        wake_texts=[], send_json=send_json, tts_frames_total=lambda: frames["v"],
        clock=clock, config=config)
    encoder = ImaAdpcmEncoder()

    await session.feed_adpcm(encoder.encode_block(_block(0)))  # 奠基準
    assert _face_states(send_json)[-1] == "idle"

    frames["v"] += 1
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json)[-1] == "idle"

    clock.advance(config.face_speak_delay_s - 0.1)
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json)[-1] == "idle"

    clock.advance(0.2)
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json)[-1] == "speak"

    clock.advance(config.speak_hold_s + 0.1)
    await session.feed_adpcm(encoder.encode_block(_block(0)))
    assert _face_states(send_json)[-1] == "idle"


# ── 16. face 訊息送出丟例外時，主流程（duck/deliver）照常 ───────────────────
@pytest.mark.asyncio
async def test_face_send_failure_does_not_break_main_flow():
    async def _send_json(msg):
        if msg.get("type") == "face":
            raise RuntimeError("boom")

    send_json = AsyncMock(side_effect=_send_json)
    deliver = AsyncMock()
    confirm_stt = AsyncMock(return_value="黑馬威下一首")
    session, model = _make_session(
        wake_texts=["馬文下一首"], send_json=send_json, confirm_stt=confirm_stt, deliver=deliver)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32, value=1000)
    await _flush()
    assert session.state == "capture"
    send_json.assert_any_await({"type": "duck"})

    await _feed_n(session, encoder, 22, value=1000)
    await _flush()

    deliver.assert_awaited_once_with("狗與露", "馬文，下一首")
    assert session.state == "reply"


# ── 「嘿馬文」喊完停下來等 ack，再說指令：那段的 speech_end 不能收尾 ─────────────

def _make_paused_session(probs, confirm_stt):
    loop = asyncio.get_event_loop()
    return PuckMicSession(
        device="test_dev", speaker="狗與露", whisper_model=FakeWhisperModel(["黑馬文"]), loop=loop,
        send_json=AsyncMock(), confirm_stt=confirm_stt, deliver=AsyncMock(),
        tts_frames_total=lambda: 0, clock=lambda: 0.0,
        vad=FakeVAD(probs), config=PuckMicConfig(), wake_engine="whisper",
    )


@pytest.mark.asyncio
async def test_wake_only_waits_for_next_segment():
    confirm_stt = AsyncMock(return_value="黑馬文下一首")
    session = _make_paused_session(
        [0.9] * 32 + [0.1] * 22 + [0.1] * 10 + [0.9] * 40 + [0.1] * 22, confirm_stt)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32)
    await _flush()
    assert session.state == "capture"

    await _feed_n(session, encoder, 22 + 10)   # 「嘿馬文」那段結束 + 停頓等 ack
    await _flush()
    assert session.state == "capture"
    confirm_stt.assert_not_awaited()

    await _feed_n(session, encoder, 40 + 22)   # 「下一首」這段開始又結束
    await _flush()
    confirm_stt.assert_awaited_once()


@pytest.mark.asyncio
async def test_wake_only_gives_up_after_wait():
    confirm_stt = AsyncMock(return_value="黑馬文")
    session = _make_paused_session([0.9] * 32 + [0.1] * 400, confirm_stt)
    encoder = ImaAdpcmEncoder()

    await _feed_n(session, encoder, 32)
    await _flush()
    await _feed_n(session, encoder, 150)       # 4.8s：還在等
    await _flush()
    assert session.state == "capture"

    await _feed_n(session, encoder, 10)        # 過 5s 等待上限 → 收尾送確認
    await _flush()
    confirm_stt.assert_awaited_once()


# ── MARVIN_PUCK_DUMP_DIR：收到的 PCM 原樣存成 16k mono WAV，close() 收檔 ──────────
@pytest.mark.asyncio
async def test_dump_dir_writes_decoded_pcm_wav(tmp_path, monkeypatch):
    from ima_adpcm import decode_block
    monkeypatch.setenv("MARVIN_PUCK_DUMP_DIR", str(tmp_path))
    session, _ = _make_session(wake_texts=[])
    enc = ImaAdpcmEncoder()
    blocks = [enc.encode_block(_block(v)) for v in (500, 1500, -800)]
    await session.feed_adpcm(b"".join(blocks))
    await session.close()

    files = list(tmp_path.glob("test_dev_*.wav"))
    assert len(files) == 1
    with wave.open(str(files[0]), "rb") as wf:
        assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 16000)
        got = wf.readframes(wf.getnframes())
    assert got == b"".join(decode_block(b).tobytes() for b in blocks)
