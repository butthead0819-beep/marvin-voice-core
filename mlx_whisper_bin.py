#!/usr/bin/env python3
"""
MLX Whisper subprocess wrapper.

Usage: python mlx_whisper_bin.py <wav_path>

Reads WAV file, runs mlx-whisper transcription, prints result to stdout.
Exit 0 on success (even if transcript is empty), non-zero on error.

Designed to be spawned as a subprocess so the parent can kill() it on timeout
without leaving zombie threads — unlike asyncio.to_thread which cannot be killed.

Model is read from MLX_WHISPER_MODEL env var (default: mlx-community/whisper-base-mlx-8bit).
"""
import os
import sys


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
        result = mlx_whisper.transcribe(
            wav_path,
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
