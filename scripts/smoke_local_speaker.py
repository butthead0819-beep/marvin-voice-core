"""Phase 1 smoke harness：本機輸出鏈端到端驗證。

Proves the full chain:
    LocalMixingAudioSource → MixerPlaybackAdapter → LocalSpeakerDevice → Mac 喇叭

Usage:
    # dry 模式（無音訊硬體，預設）：
    venv_simon/bin/python scripts/smoke_local_speaker.py

    # real 模式（真 sounddevice，親耳聽 Mac 喇叭）：
    venv_simon/bin/python scripts/smoke_local_speaker.py --real

    # 自備音檔（--real 可選）：
    venv_simon/bin/python scripts/smoke_local_speaker.py --real --file path/to/audio.mp3

Note: 聽的時候若明顯半速/斷續 = ③c-i 泵的阻塞 write+sleep 計時要調
      (LocalSpeakerDevice.frame_duration / sounddevice write blocking)。
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from local_mixing_source import (
    LocalMixingAudioSource,
    MixerPlaybackAdapter,
    ensure_mixer_playing,
    SAMPLE_RATE,
    CHANNELS,
)
from marvin_voice_core.playback_device import LocalSpeakerDevice

logger = logging.getLogger(__name__)

_REAL_MARGIN_S = 1.0  # real 模式：TTS 時長後的額外等待邊際


def play_f32_through_local_chain(
    f32_buffer: np.ndarray,
    output,
    duration_s: float,
    *,
    frame_duration: float = 0.02,
) -> None:
    """Feed f32_buffer through LocalMixingAudioSource → MixerPlaybackAdapter → LocalSpeakerDevice(output).

    Blocks for duration_s then calls device.stop().
    Pass output=None for real sounddevice (LocalSpeakerDevice lazy-imports it).
    Pass frame_duration=0 for tests (no sleep, tight pump).
    """
    mixer = LocalMixingAudioSource()
    pushed = mixer.push_tts(f32_buffer)
    if not pushed:
        raise RuntimeError("[smoke] push_tts 拒絕 buffer（超出 cap？）")
    device = LocalSpeakerDevice(output=output, frame_duration=frame_duration)
    ensure_mixer_playing(device, lambda: MixerPlaybackAdapter(mixer))
    time.sleep(duration_s)
    device.stop()


def _ffmpeg_to_f32(src_path: str) -> np.ndarray:
    """Decode audio file → 48k stereo f32 interleaved via ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-ac", str(CHANNELS),
        "-ar", str(SAMPLE_RATE),
        "-f", "f32le",
        "pipe:1",
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def _say_to_file(text: str, out_path: str) -> None:
    """macOS say: synthesize text to AIFF file (offline, no network)."""
    subprocess.run(["say", "-o", out_path, text], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1 smoke: LocalMixingAudioSource → LocalSpeakerDevice",
        epilog=(
            "venv_simon/bin/python scripts/smoke_local_speaker.py --real\n"
            "venv_simon/bin/python scripts/smoke_local_speaker.py --real --file audio.mp3\n\n"
            "半速/斷續 → LocalSpeakerDevice frame_duration 計時要調（③c-i）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--real", action="store_true",
                        help="走真 sounddevice OutputStream（需音訊硬體）")
    parser.add_argument("--file", metavar="PATH",
                        help="自備音檔（ffmpeg 支援的任何格式）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    tmp_aiff: str | None = None
    try:
        if args.file:
            src_path = args.file
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".aiff", delete=False)
            tmp.close()
            tmp_aiff = tmp.name
            print(f"[smoke] macOS say 合成 → {tmp_aiff}", flush=True)
            _say_to_file("你好，我是馬文", tmp_aiff)
            src_path = tmp_aiff

        print(f"[smoke] 解碼 {src_path} → 48k 立體聲 f32 ...", flush=True)
        f32_buf = _ffmpeg_to_f32(src_path)
        tts_duration_s = f32_buf.size / (SAMPLE_RATE * CHANNELS)

        if args.real:
            output = None  # LocalSpeakerDevice lazy-imports sounddevice
            frame_duration = 0.02
            wait_s = tts_duration_s + _REAL_MARGIN_S
            print(f"[smoke] 模式=real（sounddevice），TTS={tts_duration_s:.2f}s，等待={wait_s:.2f}s", flush=True)
        else:
            _dry_count = [0]

            class _DrySink:
                def write(self, frame: bytes) -> None:
                    _dry_count[0] += 1

                def close(self) -> None:
                    print(f"[smoke] dry sink 關閉，共收 {_dry_count[0]} 幀", flush=True)

            output = _DrySink()
            frame_duration = 0.0
            wait_s = tts_duration_s + 0.1
            print(f"[smoke] 模式=dry（無音訊硬體），TTS={tts_duration_s:.2f}s", flush=True)

        play_f32_through_local_chain(f32_buf, output=output, duration_s=wait_s, frame_duration=frame_duration)
        print("[smoke] 完成。", flush=True)

    finally:
        if tmp_aiff:
            Path(tmp_aiff).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
