"""
tests/test_browser_speaker_output.py
TDD：純軟體 satellite 的輸出 tee（BrowserSpeakerOutput）。

與 WyomingSpeakerOutput 對稱（write(48k stereo s16 frame)/close()），但不送網路，
而是用靜音偵測把 mixer 泵的連續幀切成「一段回覆」，供 /reply 給瀏覽器播。
純邏輯、無執行緒/網路依賴，用合成幀驗證切句。
"""
import struct
import pytest


def _frame(amp: int, n_samples: int = 960) -> bytes:
    """造 48k stereo s16 幀（n_samples 對 stereo＝n_samples*2 個 int16）；amp=0＝靜音。"""
    return struct.pack("<%dh" % (n_samples * 2), *([amp] * (n_samples * 2)))


def _wav_data_len(wav: bytes) -> int:
    """回 WAV data chunk 的 byte 數（驗證累積了多少音訊）。"""
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    idx = wav.find(b"data")
    return struct.unpack("<I", wav[idx + 4: idx + 8])[0]


def test_no_audio_yet_returns_seq_zero():
    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    out = BrowserSpeakerOutput()
    seq, wav = out.latest_wav()
    assert seq == 0
    assert wav == b""


def test_speech_then_silence_finalizes_one_reply():
    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    out = BrowserSpeakerOutput(hangover_frames=3)
    for _ in range(5):
        out.write(_frame(8000))          # 5 幀語音
    seq0, _ = out.latest_wav()
    assert seq0 == 0                      # 還沒靜音夠久，未定案
    for _ in range(3):
        out.write(_frame(0))             # 靜音達 hangover → 定案
    seq1, wav = out.latest_wav()
    assert seq1 == 1
    assert _wav_data_len(wav) == 5 * 960 * 2 * 2   # 5 幀 stereo s16


def test_two_replies_bump_seq_separately():
    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    out = BrowserSpeakerOutput(hangover_frames=2)
    for _ in range(3): out.write(_frame(8000))
    for _ in range(2): out.write(_frame(0))
    assert out.latest_wav()[0] == 1
    for _ in range(4): out.write(_frame(6000))
    for _ in range(2): out.write(_frame(0))
    seq, wav = out.latest_wav()
    assert seq == 2
    assert _wav_data_len(wav) == 4 * 960 * 2 * 2   # 第二段只含第二段音訊，未沾第一段


def test_brief_silence_within_reply_does_not_split():
    """回覆中間的短暫停頓（< hangover）不切段，仍算同一段。"""
    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    out = BrowserSpeakerOutput(hangover_frames=4)
    for _ in range(3): out.write(_frame(8000))
    for _ in range(2): out.write(_frame(0))    # 短停頓，不到 hangover
    for _ in range(3): out.write(_frame(8000))
    for _ in range(4): out.write(_frame(0))    # 這次夠久 → 定案
    seq, _ = out.latest_wav()
    assert seq == 1                            # 只切成一段，不是兩段


def test_pure_silence_never_finalizes():
    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    out = BrowserSpeakerOutput(hangover_frames=2)
    for _ in range(20):
        out.write(_frame(0))
    assert out.latest_wav()[0] == 0            # 全靜音＝沒有回覆


def test_close_flushes_pending_audio():
    """close() 時若有未定案的音訊（尾巴沒靜音），也要定案，不遺失最後一句。"""
    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    out = BrowserSpeakerOutput(hangover_frames=5)
    for _ in range(4): out.write(_frame(8000))
    assert out.latest_wav()[0] == 0            # 還沒靜音夠
    out.close()
    assert out.latest_wav()[0] == 1            # close 強制 flush


def test_write_ignores_empty_frame():
    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    out = BrowserSpeakerOutput(hangover_frames=2)
    out.write(b"")                             # 不炸、不算音訊
    for _ in range(2): out.write(_frame(0))
    assert out.latest_wav()[0] == 0
