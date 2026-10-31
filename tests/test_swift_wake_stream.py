"""SwiftWakeStreamDetector — 用假 Swift 程序（純 Python 腳本，講同一套 stdin/stdout
framing）驗證常駐子程序管理、ready 門檻、result 行解析、watermark 去重、重啟。

不在測試裡複製 detector 的邏輯來驗算：假程序只依腳本預排的「收到第 N 個 frame 後
印哪些 result 行」回應，斷言只看外部可觀察行為（callback 有沒有被呼叫、呼叫幾次、
帶的 text；假程序實際收到的 frame type/length/內容，經 frame_log 記錄）。
"""
from __future__ import annotations

import asyncio
import json
import sys

import numpy as np
import pytest
from unittest.mock import AsyncMock

from ima_adpcm import BLOCK_SAMPLES, ImaAdpcmEncoder
from puck_wake import pinyin_wake_match
from swift_wake_stream import SwiftWakeStreamDetector

# 假 Swift 程序：讀 stdin framing（1 byte type + 4 byte LE length + payload），
# 啟動先印 ready，每收完一個 frame 就記錄到 frame_log（若指定），並依
# script["emit"][str(frame_count)] 印出預排的 result 行（str/dict 皆可，
# dict 含 "_raw" 鍵表示印原始文字而非 json 編碼——用來模擬壞 JSON 行）。
# script["crash_once_marker"]：第一次啟動（marker 不存在）收到第 1 個 frame 就
# exit(1)，並建立 marker；第二次啟動（marker 已存在）正常運作，用來模擬子程序
# 中途掛掉、detector 重啟後接上新程序。
FAKE_SCRIPT = r'''
import sys, struct, json, os

def read_exact(n):
    buf = b""
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

script = json.loads(sys.argv[1])
marker = script.get("crash_once_marker")
will_crash = False
if marker:
    if not os.path.exists(marker):
        open(marker, "w").close()
        will_crash = True

log_path = script.get("frame_log")
log_f = open(log_path, "a") if log_path else None

print(json.dumps({"type": "ready"}))
sys.stdout.flush()

emit_map = script.get("emit", {})
frame_count = 0

while True:
    t = sys.stdin.buffer.read(1)
    if not t:
        break
    lenb = read_exact(4)
    if lenb is None:
        break
    length = struct.unpack("<I", lenb)[0]
    payload = read_exact(length)
    if payload is None:
        break
    frame_count += 1

    if log_f is not None:
        log_f.write(json.dumps({
            "type": t.decode(errors="replace"),
            "length": length,
            "payload_hex": payload.hex(),
        }) + "\n")
        log_f.flush()

    if will_crash and frame_count == 1:
        sys.exit(1)

    for emit in emit_map.get(str(frame_count), []):
        if isinstance(emit, dict) and "_raw" in emit:
            print(emit["_raw"])
        else:
            print(json.dumps(emit))
        sys.stdout.flush()

if log_f is not None:
    log_f.close()
'''


def _make_cmd(script: dict) -> list:
    return [sys.executable, "-c", FAKE_SCRIPT, json.dumps(script)]


async def _wait_ready(detector, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not detector.ready and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert detector.ready, "fake 子程序未在時限內 ready"


def _read_log(path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── 1. ready 門檻 + frame framing（type/length/內容） ─────────────────────────
@pytest.mark.asyncio
async def test_framing_ready_gate_and_frame_shapes(tmp_path):
    log_path = tmp_path / "frames.log"
    cmd = _make_cmd({"frame_log": str(log_path)})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append((user_id, first_audio_time, text))

    det = SwiftWakeStreamDetector(on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match)
    try:
        # 構造後、await 前：subprocess 尚未真的啟動，_ready 必為 False。
        det.on_speech_start(0, 1.23, pre_roll=b"")
        det.push_pcm(0, b"\x01\x02\x03\x04")  # ready 前 push → 直接丟棄，不送出
        assert _read_log(log_path) == []

        await _wait_ready(det)
        assert _read_log(log_path) == []  # ready 前丟的那塊，從未寫出

        payload = b"\x05\x06\x07\x08"
        det.push_pcm(0, payload)
        det.on_speech_end(0)

        await asyncio.sleep(0.3)
        frames = _read_log(log_path)
        assert len(frames) == 2
        assert frames[0] == {"type": "A", "length": 4, "payload_hex": payload.hex()}
        assert frames[1] == {"type": "F", "length": 0, "payload_hex": ""}
        assert calls == []
    finally:
        await det.aclose()


# ── 2. 含喚醒詞的 volatile result → callback 被呼叫一次 ──────────────────────
@pytest.mark.asyncio
async def test_wake_trigger_basic():
    result_line = {"type": "result", "final": False, "start": 0.0, "end": 1.0,
                   "text": "馬文開燈", "runs": [["馬文開燈", 0.1, 1.0]]}
    cmd = _make_cmd({"emit": {"1": [result_line]}})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append((user_id, first_audio_time, text))

    det = SwiftWakeStreamDetector(on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match)
    try:
        await _wait_ready(det)
        det.on_speech_start(0, 2.5)
        det.push_pcm(0, b"\x00\x00" * 10)
        await asyncio.sleep(0.3)

        assert calls == [(0, 2.5, "馬文開燈")]
        assert det.stats["fires"] == 1
        assert det.stats["results"] == 1
    finally:
        await det.aclose()


# ── 3. 同一句喚醒被 volatile 改寫（run start < 上次 watermark）→ rearm 後不再觸發 ─
@pytest.mark.asyncio
async def test_same_utterance_rewrite_not_retriggered_after_rearm():
    r1 = {"type": "result", "final": False, "start": 0.0, "end": 1.0,
          "text": "馬文", "runs": [["馬文", 0.4, 1.0]]}
    r2 = {"type": "result", "final": True, "start": 0.0, "end": 1.2,
          "text": "馬文", "runs": [["馬文", 0.5, 1.2]]}
    cmd = _make_cmd({"emit": {"1": [r1], "2": [r2]}})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append(text)

    det = SwiftWakeStreamDetector(on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match)
    try:
        await _wait_ready(det)
        det.on_speech_start(0, 0.0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["馬文"]

        det.rearm(0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["馬文"]  # run start 0.5 < watermark(1.0) → 不再觸發
    finally:
        await det.aclose()


# ── 4. rearm 後，wake run start >= watermark 的新喚醒 → 再觸發 ────────────────
@pytest.mark.asyncio
async def test_rearm_then_new_wake_at_or_after_watermark_triggers():
    r1 = {"type": "result", "final": True, "start": 0.0, "end": 1.0,
          "text": "馬文", "runs": [["馬文", 0.4, 1.0]]}
    r2 = {"type": "result", "final": True, "start": 1.0, "end": 2.0,
          "text": "馬文", "runs": [["馬文", 1.0, 2.0]]}
    cmd = _make_cmd({"emit": {"1": [r1], "2": [r2]}})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append(text)

    det = SwiftWakeStreamDetector(on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match)
    try:
        await _wait_ready(det)
        det.on_speech_start(0, 0.0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["馬文"]

        det.rearm(0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["馬文", "馬文"]
    finally:
        await det.aclose()


# ── 5. 未 rearm 時，同一批次裡第二個喚醒不觸發（_fired 擋住） ─────────────────
@pytest.mark.asyncio
async def test_without_rearm_second_wake_in_same_batch_blocked():
    r1 = {"type": "result", "final": True, "start": 0.0, "end": 1.0,
          "text": "馬文", "runs": [["馬文", 0.4, 1.0]]}
    r2 = {"type": "result", "final": True, "start": 1.0, "end": 2.0,
          "text": "馬文", "runs": [["馬文", 1.0, 2.0]]}
    cmd = _make_cmd({"emit": {"1": [r1, r2]}})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append(text)

    det = SwiftWakeStreamDetector(on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match)
    try:
        await _wait_ready(det)
        det.on_speech_start(0, 0.0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["馬文"]  # r2 跟 r1 同批次抵達，_fired 已 True → r2 被擋
    finally:
        await det.aclose()


# ── 6. 沒喚醒詞的 result 不觸發；壞 JSON 行不讓 detector 掛掉 ─────────────────
@pytest.mark.asyncio
async def test_no_match_result_ignored_and_bad_json_tolerated():
    no_match = {"type": "result", "final": True, "start": 0.0, "end": 1.0,
                "text": "今天天氣真好", "runs": [["今天天氣真好", 0.0, 1.0]]}
    good = {"type": "result", "final": True, "start": 1.0, "end": 2.0,
            "text": "馬文", "runs": [["馬文", 1.0, 2.0]]}
    cmd = _make_cmd({"emit": {
        "1": [{"_raw": "{not valid json"}, no_match],
        "2": [good],
    }})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append(text)

    det = SwiftWakeStreamDetector(on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match)
    try:
        await _wait_ready(det)
        det.on_speech_start(0, 0.0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == []

        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["馬文"]
    finally:
        await det.aclose()


# ── 7. 假程序中途 exit → detector 重啟；重啟後 watermark 重設 ────────────────
@pytest.mark.asyncio
async def test_restart_on_crash_resets_watermark(tmp_path):
    marker = tmp_path / "crash_marker"
    wake_result = {"type": "result", "final": True, "start": 0.0, "end": 1.0,
                   "text": "馬文", "runs": [["馬文", 0.5, 1.0]]}
    cmd = _make_cmd({"crash_once_marker": str(marker), "emit": {"1": [wake_result]}})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append(text)

    det = SwiftWakeStreamDetector(
        on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match,
        restart_backoff_s=0.05, max_backoff_s=0.2,
    )
    try:
        await _wait_ready(det)
        det.on_speech_start(0, 0.0)
        det.push_pcm(0, b"\x00\x00")  # 第一代：收到第 1 個 frame 就 crash（marker 不存在）

        deadline = asyncio.get_running_loop().time() + 5.0
        while det.stats["restarts"] < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert det.stats["restarts"] == 1
        assert calls == []  # 第一代沒機會 emit

        await _wait_ready(det)  # 第二代 ready
        det.push_pcm(0, b"\x00\x00")  # 第二代：收到第 1 個 frame → emit wake_t=0.5
        await asyncio.sleep(0.3)
        assert calls == ["馬文"]
    finally:
        await det.aclose()


# ── 8. aclose() 後程序結束、不再重啟 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_aclose_stops_process_and_no_restart():
    cmd = _make_cmd({})
    loop = asyncio.get_running_loop()

    async def on_wake(user_id, first_audio_time, text):
        pass

    det = SwiftWakeStreamDetector(
        on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match,
        restart_backoff_s=0.05, max_backoff_s=0.2,
    )
    await _wait_ready(det)
    await det.aclose()
    await asyncio.sleep(0.3)

    assert det.ready is False
    assert det.stats["restarts"] == 0

    await det.aclose()  # 可重複呼叫


# ── 9. PuckMicSession(wake_engine="swift") → 候選喚醒進 capture + 送 duck ─────
@pytest.mark.asyncio
async def test_puck_mic_session_swift_engine_triggers_duck(tmp_path, monkeypatch):
    import puck_mic_session as pms

    wake_result = {"type": "result", "final": True, "start": 0.0, "end": 1.0,
                   "text": "馬文", "runs": [["馬文", 0.1, 1.0]]}
    cmd = _make_cmd({"emit": {"1": [wake_result]}})

    def _fake_ctor(on_wake_callback, loop, **kwargs):
        return SwiftWakeStreamDetector(on_wake_callback, loop, cmd=cmd, wake_matcher=pinyin_wake_match)

    monkeypatch.setattr(pms, "SwiftWakeStreamDetector", _fake_ctor)

    class FakeVAD:
        def __init__(self, probs):
            self._probs = list(probs)
            self._idx = 0

        def process(self, pcm16):
            n_windows = len(pcm16) // 512
            out = []
            for _ in range(n_windows):
                out.append(self._probs[self._idx] if self._idx < len(self._probs) else 0.0)
                self._idx += 1
            return out

    send_json = AsyncMock()
    loop = asyncio.get_running_loop()
    session = pms.PuckMicSession(
        device="test_dev", speaker="狗與露", whisper_model=None, loop=loop,
        send_json=send_json,
        confirm_stt=AsyncMock(return_value=""),
        deliver=AsyncMock(),
        tts_frames_total=lambda: 0,
        clock=lambda: 0.0,
        vad=FakeVAD([0.9] * 8 + [0.9] * 24 + [0.1] * 22),
        wake_engine="swift",
    )
    try:
        await _wait_ready(session._wake)

        encoder = ImaAdpcmEncoder()
        block = np.full(BLOCK_SAMPLES, 1000, dtype=np.int16)
        data = b"".join(encoder.encode_block(block) for _ in range(32))
        await session.feed_adpcm(data)

        deadline = asyncio.get_running_loop().time() + 3.0
        while session.state != "capture" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert session.state == "capture"
        send_json.assert_any_await({"type": "duck"})
    finally:
        await session.close()


# ── 10. 真 SpeechAnalyzer 的 volatile 只有單一 run、range start 不動：同一 range 內
#        rearm 後文字長出第二個喚醒 → 觸發；只是改寫（次數沒增加）→ 不觸發 ─────────
@pytest.mark.asyncio
async def test_same_range_second_wake_fires_rewrite_does_not():
    def vol(text, end):
        return {"type": "result", "final": False, "start": 0.0, "end": end,
                "text": text, "runs": [[text, 0.0, end]]}
    cmd = _make_cmd({"emit": {
        "1": [vol("黑馬文", 2.0)],
        "2": [vol("黑馬文下一首", 3.0)],
        "3": [vol("黑馬文下一首馬文", 5.0)],
    }})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append(text)

    det = SwiftWakeStreamDetector(on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match)
    try:
        await _wait_ready(det)
        det.on_speech_start(0, 0.0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["黑馬文"]

        det.rearm(0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["黑馬文"]  # 同 range 改寫、喚醒次數沒增加

        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["黑馬文", "黑馬文下一首馬文"]
    finally:
        await det.aclose()


# ── 11. final 把舊範圍切開、新 range start 早於上次觸發時聽到的位置 → 裡面已觸發過
#        的喚醒不重觸發；之後新增的喚醒才觸發 ───────────────────────────────────
@pytest.mark.asyncio
async def test_split_range_does_not_refire_old_wake():
    cmd = _make_cmd({"emit": {
        "1": [{"type": "result", "final": False, "start": 0.0, "end": 7.0,
               "text": "嗯嗯黑馬文下一首", "runs": [["嗯嗯黑馬文下一首", 0.0, 7.0]]}],
        "2": [{"type": "result", "final": True, "start": 0.0, "end": 2.0,
               "text": "嗯嗯", "runs": [["嗯", 0.0, 1.0], ["嗯", 1.0, 2.0]]},
              {"type": "result", "final": False, "start": 2.0, "end": 7.5,
               "text": "黑馬文下一首", "runs": [["黑馬文下一首", 2.0, 7.5]]}],
        "3": [{"type": "result", "final": False, "start": 2.0, "end": 9.0,
               "text": "黑馬文下一首馬文", "runs": [["黑馬文下一首馬文", 2.0, 9.0]]}],
    }})
    loop = asyncio.get_running_loop()
    calls = []

    async def on_wake(user_id, first_audio_time, text):
        calls.append(text)

    det = SwiftWakeStreamDetector(on_wake, loop, cmd=cmd, wake_matcher=pinyin_wake_match)
    try:
        await _wait_ready(det)
        det.on_speech_start(0, 0.0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls == ["嗯嗯黑馬文下一首"]

        det.rearm(0)
        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert len(calls) == 1  # start 2.0 < 上次觸發 end 7.0 → 舊喚醒視為用掉

        det.push_pcm(0, b"\x00\x00")
        await asyncio.sleep(0.3)
        assert calls[-1] == "黑馬文下一首馬文" and len(calls) == 2
    finally:
        await det.aclose()
