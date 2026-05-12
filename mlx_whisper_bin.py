#!/usr/bin/env python3
"""
MLX Whisper subprocess wrapper.

Usage: python mlx_whisper_bin.py <wav_path>

Reads WAV file, converts Discord format (48kHz stereo int16) → 16kHz mono float32,
then runs mlx-whisper transcription and prints result to stdout.

Exit 0 on success (even if transcript is empty), non-zero on error.

Designed to be spawned as a subprocess so the parent can kill() it on timeout
without leaving zombie threads — unlike asyncio.to_thread which cannot be killed.

Model is read from MLX_WHISPER_MODEL env var (default: mlx-community/whisper-base-mlx-8bit).
"""
import os
import sys
import wave


def _load_discord_wav_as_float32(wav_path: str):
    """
    Read a Discord-format WAV (48kHz stereo int16) and return a 16kHz mono float32 array.
    Discord sends 48kHz/stereo/16-bit. Whisper expects 16kHz/mono/float32.
    Conversion: average stereo → mono, then decimate 3:1 (48000/16000=3).
    """
    import numpy as np
    with wave.open(wav_path) as w:
        n_ch = w.getnchannels()
        rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)

    # Stereo → mono
    if n_ch == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)

    # Normalize int16 to [-1, 1]
    samples /= 32768.0

    # Resample to 16kHz via integer decimation (only works cleanly for 48→16)
    if rate != 16000:
        factor = rate // 16000
        if factor > 1:
            samples = samples[::factor]
        # If rate is already 16kHz or non-integer ratio, pass through

    return samples.astype(np.float32)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: mlx_whisper_bin.py <wav_path>", file=sys.stderr)
        return 1

    wav_path = sys.argv[1]

    if not os.path.exists(wav_path):
        print(f"Error: file not found: {wav_path}", file=sys.stderr)
        return 1

    model_repo = os.getenv("MLX_WHISPER_MODEL", "mlx-community/whisper-base-mlx-8bit")

    try:
        import mlx_whisper
    except ImportError:
        print("Error: mlx-whisper not installed (pip install mlx-whisper)", file=sys.stderr)
        return 1

    try:
        audio = _load_discord_wav_as_float32(wav_path)
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=model_repo,
            language="zh",
            verbose=False,
        )
        text = (result.get("text") or "").strip()
        if text:
            print(text)
        return 0
    except Exception as e:
        print(f"Error: transcription failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
