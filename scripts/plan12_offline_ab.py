#!/usr/bin/env python3
"""Plan 12 離線 A/B render — 驗 f32 本地混音音質能否追平 ffmpeg 烤音量。

Path A（對照組）= 現行串流路徑 voice_controller.py:7083：
    ffmpeg -af loudnorm=I=-14:TP=-1.5:LRA=11,volume=<v>  → s16le WAV
Path B（Plan 12）=
    ffmpeg -af loudnorm=I=-14:TP=-1.5:LRA=11 → f32le → numpy*<v> → TPDF dither → s16le WAV

兩條共用同一份 loudnorm 與 sample rate / channels，差異只在「增益在量化前 vs 量化後」。
"""
from __future__ import annotations

import argparse
import random
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

SONGS_DIR = Path(__file__).resolve().parents[1] / "assets" / "songs"
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
SR = 48000
CHANNELS = 2
VOLUMES = [0.10, 0.30]


def pick_song(seed: int | None) -> tuple[Path, int]:
    songs = sorted(SONGS_DIR.glob("*.mp3"))
    if not songs:
        raise SystemExit(f"找不到 mp3：{SONGS_DIR}")
    if seed is None:
        seed = random.randint(0, 99999)
    return random.Random(seed).choice(songs), seed


def render_path_a(song: Path, volume: float, out_wav: Path) -> None:
    af = f"{LOUDNORM},volume={volume:.3f}"
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "warning",
        "-i", str(song),
        "-vn", "-ac", str(CHANNELS), "-ar", str(SR),
        "-af", af,
        "-c:a", "pcm_s16le",
        str(out_wav),
    ]
    subprocess.run(cmd, check=True)


def render_path_b(song: Path, volume: float, out_wav: Path) -> None:
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "warning",
        "-i", str(song),
        "-vn", "-ac", str(CHANNELS), "-ar", str(SR),
        "-af", LOUDNORM,
        "-f", "f32le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.buffer.write(proc.stderr)
        raise SystemExit(f"ffmpeg failed on Path B for {song.name}")

    f32 = np.frombuffer(proc.stdout, dtype=np.float32)
    if f32.size % CHANNELS:
        f32 = f32[: f32.size - (f32.size % CHANNELS)]
    f32 = f32.reshape(-1, CHANNELS)

    gained = f32 * np.float32(volume)

    # TPDF dither：兩個獨立 Uniform(-0.5, +0.5) LSB 相加 = 三角分布 [-1, +1] LSB
    rng = np.random.default_rng(0xD17 ^ int(volume * 1000))
    lsb = np.float32(1.0 / 32768.0)
    d1 = rng.uniform(-0.5, 0.5, size=gained.shape).astype(np.float32)
    d2 = rng.uniform(-0.5, 0.5, size=gained.shape).astype(np.float32)
    dithered = gained + (d1 + d2) * lsb

    s16 = np.clip(np.round(dithered * 32768.0), -32768, 32767).astype(np.int16)

    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(s16.tobytes())


def sanitize(stem: str) -> str:
    keep = "-_."
    out = "".join(c if c.isalnum() or c in keep else "_" for c in stem)
    return out.strip("_") or "song"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--song", type=Path, default=None, help="指定歌曲；不給就從 assets/songs 隨機挑")
    ap.add_argument("--seed", type=int, default=None, help="隨機種子；不給用 time 隨機")
    ap.add_argument("--out", type=Path, default=None, help="輸出目錄；不給就用 tmp")
    args = ap.parse_args()

    if args.song:
        song = args.song
        if not song.exists():
            raise SystemExit(f"找不到歌：{song}")
        print(f"🎵 指定曲：{song.name}")
    else:
        song, seed = pick_song(args.seed)
        print(f"🎲 seed={seed}  選曲：{song.name}")

    out_dir = args.out or Path(tempfile.mkdtemp(prefix="plan12_ab_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = sanitize(song.stem)

    print(f"📂 輸出：{out_dir}\n")
    for v in VOLUMES:
        tag = f"v{int(v * 100):02d}"
        out_a = out_dir / f"{stem}_A_baked_{tag}.wav"
        out_b = out_dir / f"{stem}_B_f32_{tag}.wav"

        t0 = time.time()
        render_path_a(song, v, out_a)
        ta = time.time() - t0
        print(f"  ✅ Path A (ffmpeg 烤 vol={v:.2f})  {ta:5.1f}s  →  {out_a.name}")

        t0 = time.time()
        render_path_b(song, v, out_b)
        tb = time.time() - t0
        print(f"  ✅ Path B (f32 本地  vol={v:.2f})  {tb:5.1f}s  →  {out_b.name}\n")

    print("🎧 A/B：用任何 WAV 播放器同時開 A 與 B（同音量），盲聽切換。")
    print("    重點：低音量段（副歌轉安靜處）有沒有沙沙底噪、量化粗糙感、低頻糊掉。")
    print("    Plan 12 預期：B 聽起來『跟 A 一樣乾淨』，不是『比 A 好』。")


if __name__ == "__main__":
    main()
