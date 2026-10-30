"""離線重播工具 — 模擬 puck 上傳路徑，驗證 PuckVADStream + WakeStreamDetector。

用法：
  venv_simon/bin/python tools/puck_vad_replay.py <wav|mp3> [--no-whisper] [--start-prob 0.5] [--end-prob 0.35]
    [--pinyin] [--rearm] [--start SEC] [--duration SEC]

流程：讀 WAV/MP3 → 重取樣到 16kHz mono → 切 512-sample block → IMA ADPCM
編碼再解碼（模擬 puck 上傳/下傳）→ 餵進 PuckVADStream(SileroStreamVAD())，
除非 --no-whisper 否則同時餵進真的 WakeStreamDetector（16k mono 路徑）。

注意：WakeStreamDetector 的推理是 asyncio.to_thread，離線重播餵資料的速度
遠快於即時（沒有真的等待音訊時間），所以 inflight 期間被 push 的視窗會被
WakeStreamDetector 直接丟棄（這是現有設計的預期行為，不是這個工具的 bug）。
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ima_adpcm import BLOCK_SAMPLES, ImaAdpcmEncoder, decode_block
from puck_vad_stream import PuckVADConfig, PuckVADStream
from puck_wake import pinyin_wake_match
from silero_stream_vad import SileroStreamVAD


def load_wav_as_16k_mono(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        rate = wf.getframerate()
        sampwidth = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth != 2:
        raise ValueError(f"只支援 16-bit PCM wav，got sampwidth={sampwidth * 8}bit")

    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if n_channels > 1:
        arr = arr.reshape(-1, n_channels).mean(axis=1)

    if rate != 16000:
        duration = len(arr) / rate
        n_out = int(round(duration * 16000))
        x_old = np.linspace(0, duration, num=len(arr), endpoint=False)
        x_new = np.linspace(0, duration, num=n_out, endpoint=False)
        arr = np.interp(x_new, x_old, arr)

    return arr.astype(np.int16)


def load_mp3_as_16k_mono(path: str, start: float | None, duration: float | None) -> np.ndarray:
    cmd = ["ffmpeg", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-i", path, "-ar", "16000", "-ac", "1", "-f", "s16le", "-"]
    result = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype=np.int16)


def load_audio_as_16k_mono(path: str, start: float | None, duration: float | None) -> np.ndarray:
    if not path.lower().endswith(".wav"):
        return load_mp3_as_16k_mono(path, start, duration)
    arr = load_wav_as_16k_mono(path)
    if start is not None:
        arr = arr[int(round(start * 16000)):]
    if duration is not None:
        arr = arr[:int(round(duration * 16000))]
    return arr


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("wav")
    p.add_argument("--no-whisper", action="store_true")
    p.add_argument("--start-prob", type=float, default=0.5)
    p.add_argument("--end-prob", type=float, default=0.35)
    p.add_argument("--realtime", action="store_true",
                   help="照真實時間餵（每 block 32ms），推理次數/inflight 丟幀才跟線上一致")
    p.add_argument("--show-infer", action="store_true", help="每次 Whisper 推理都印出文字")
    p.add_argument("--pinyin", action="store_true",
                   help="無 prompt + 拼音比對設定（initial_prompt=None, temperature=0.0, window=1.0s, stride=0.25s）")
    p.add_argument("--rearm", action="store_true",
                   help="WAKE callback 後呼叫 wake.rearm(0)，模擬候選被否決後繼續偵測")
    p.add_argument("--start", type=float, default=None, help="起始秒數（切片）")
    p.add_argument("--duration", type=float, default=None, help="長度秒數（切片）")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    pcm = load_audio_as_16k_mono(args.wav, args.start, args.duration)
    total_samples = len(pcm)
    total_duration = total_samples / 16000.0

    vad = SileroStreamVAD()
    config = PuckVADConfig(start_prob=args.start_prob, end_prob=args.end_prob)

    wake = None
    infer_count = 0
    wake_count = 0

    if not args.no_whisper:
        from faster_whisper import WhisperModel

        from wake_stream_detector import WakeStreamDetector

        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        loop = asyncio.get_running_loop()

        async def on_wake_callback(user_id: int, first_audio_time: float, text: str) -> None:
            nonlocal wake_count
            wake_count += 1
            t = fed_samples / 16000.0
            print(f"[{t:7.2f}s] WAKE text='{text}'")
            if args.rearm:
                wake.rearm(0)

        if args.show_infer:
            _orig_transcribe = model.transcribe

            def _logged_transcribe(*a, **kw):
                segs, info = _orig_transcribe(*a, **kw)
                segs = list(segs)
                print(f"[{fed_samples / 16000.0:7.2f}s]   infer: '{''.join(x.text for x in segs).strip()}'")
                return segs, info

            model.transcribe = _logged_transcribe

        if args.pinyin:
            wake = WakeStreamDetector(
                model, on_wake_callback, loop, input_rate=16000, input_channels=1,
                initial_prompt=None, temperature=0.0, window_s=1.0, stride_s=0.25,
                wake_matcher=lambda t: pinyin_wake_match(t) is not None,
            )
        else:
            wake = WakeStreamDetector(model, on_wake_callback, loop, input_rate=16000, input_channels=1)

        orig_infer = wake._infer
        infer_elapsed_total = 0.0

        async def counted_infer(*a, **kw):
            nonlocal infer_count, infer_elapsed_total
            infer_count += 1
            t0 = time.perf_counter()
            await orig_infer(*a, **kw)
            infer_elapsed_total += time.perf_counter() - t0

        wake._infer = counted_infer

    fed_samples = 0

    def on_speech_start(pre_roll: bytes) -> None:
        t = fed_samples / 16000.0
        print(f"[{t:7.2f}s] SPEECH_START")
        if wake is not None:
            wake.on_speech_start(0, t, pre_roll)

    def on_pcm(pcm_bytes: bytes) -> None:
        if wake is not None:
            wake.push_pcm(0, pcm_bytes)

    def on_speech_end(segment_pcm: bytes, reason: str) -> None:
        t = fed_samples / 16000.0
        dur = len(segment_pcm) / 2 / 16000.0
        print(f"[{t:7.2f}s] SPEECH_END reason={reason} dur={dur:.1f}s")
        if wake is not None:
            wake.on_speech_end(0)

    stream = PuckVADStream(
        vad, config,
        on_speech_start=on_speech_start,
        on_pcm=on_pcm,
        on_speech_end=on_speech_end,
    )

    encoder = ImaAdpcmEncoder()
    n_blocks = (total_samples + BLOCK_SAMPLES - 1) // BLOCK_SAMPLES
    vad_elapsed_total = 0.0

    t_start = time.perf_counter()
    for i in range(n_blocks):
        if args.realtime:
            lag = t_start + i * BLOCK_SAMPLES / 16000.0 - time.perf_counter()
            if lag > 0:
                await asyncio.sleep(lag)
        chunk = pcm[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES]
        if len(chunk) < BLOCK_SAMPLES:
            chunk = np.pad(chunk, (0, BLOCK_SAMPLES - len(chunk)))

        block = encoder.encode_block(chunk)
        decoded = decode_block(block)

        t0 = time.perf_counter()
        stream.feed(decoded)
        vad_elapsed_total += time.perf_counter() - t0

        fed_samples += BLOCK_SAMPLES

        if wake is not None:
            await asyncio.sleep(0)

    if wake is not None:
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    stats = stream.stats
    duty = stats["speech_windows"] / stats["windows"] if stats["windows"] else 0.0
    avg_vad_ms = (vad_elapsed_total / stats["windows"] * 1000) if stats["windows"] else 0.0

    print()
    print(f"總長: {total_duration:.1f}s")
    print(f"segments: {stats['segments']}")
    print(f"speech duty cycle: {duty:.1%}")
    if wake is not None:
        print(f"Whisper 推理次數: {infer_count}")
        print(f"wake 次數: {wake_count}")
        avg_infer_s = (infer_elapsed_total / infer_count) if infer_count else 0.0
        print(f"Whisper 平均推理耗時: {avg_infer_s:.2f}s")
    print(f"VAD 每 window 平均耗時: {avg_vad_ms:.2f}ms")


if __name__ == "__main__":
    asyncio.run(main())
