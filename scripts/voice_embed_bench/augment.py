"""對 clean 語音做 Discord 模擬：opus 壓縮、粉紅噪音 + opus（噪音在麥克風端，先混再壓縮）。

CLI：python -m scripts.voice_embed_bench.augment [--root ...] [--noise-dir ...]
"""
from __future__ import annotations

import argparse
import json
import random
import tempfile
from pathlib import Path

import numpy as np

from scripts.voice_embed_bench import audio_io
from scripts.voice_embed_bench.audio_io import SR

SNRS = (20, 10, 5)
_NOISE_EXTS = (".wav", ".mp3", ".flac", ".ogg")


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0


def pink_noise(n: int, seed: int) -> np.ndarray:
    """FFT 1/f 粉紅噪音，float32，RMS 正規化到 0.1。"""
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n)
    freqs[0] = freqs[1]  # 避開 DC 除以 0
    noise = np.fft.irfft(spec / np.sqrt(freqs), n).astype(np.float32)
    return (noise * (0.1 / _rms(noise))).astype(np.float32)


def fit_length(noise: np.ndarray, n: int, offset: int) -> np.ndarray:
    """從 offset 開始取 n 個樣本，不夠就循環接。"""
    return noise[(np.arange(n) + offset) % len(noise)]


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    """以指定 SNR 混噪音；爆音時整體除以 peak（保留 SNR）。靜音回傳副本。"""
    noise = fit_length(noise, len(speech), 0)
    rs, rn = _rms(speech), _rms(noise)
    if rs == 0 or rn == 0:
        return speech.copy()
    scale = rs / (rn * 10 ** (snr_db / 20))
    out = speech + scale * noise
    peak = float(np.max(np.abs(out)))
    if peak > 1.0:
        out = out / peak
    return out.astype(np.float32)


def _load_noises(noise_dir: Path | None, seed: int) -> list[np.ndarray]:
    if noise_dir is None:
        return [pink_noise(SR * 30, seed)]
    noises = []
    for p in sorted(Path(noise_dir).rglob("*")):
        if p.suffix.lower() not in _NOISE_EXTS:
            continue
        if p.suffix.lower() == ".wav":
            audio, sr = audio_io.read_wav(p)
            if sr == SR:
                noises.append(audio)
                continue
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "n.wav"
            audio_io.ffmpeg_to_wav16k(p, tmp)
            audio, _ = audio_io.read_wav(tmp)
        noises.append(audio)
    if not noises:
        raise SystemExit(f"--noise-dir 內找不到噪音檔：{noise_dir}")
    return noises


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="records/voice_embed_bench")
    ap.add_argument("--noise-dir", default=None)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    root = Path(args.root)
    with open(root / "clips_clean.jsonl", encoding="utf-8") as f:
        clean = [json.loads(line) for line in f if line.strip()]
    noises = _load_noises(Path(args.noise_dir) if args.noise_dir else None, args.seed)

    out_clips: list[dict] = []
    for clip in clean:
        src = root / clip["path"]
        speech = None
        for cond, snr in [("opus", None)] + [(f"noise{s}_opus", s) for s in SNRS]:
            rel = f"audio/{cond}/{clip['clip_id']}.wav"
            dst = root / rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if snr is None:
                    audio_io.opus_roundtrip(src, dst)
                else:
                    if speech is None:
                        speech, _ = audio_io.read_wav(src)
                    rng = random.Random(f"{args.seed}:{clip['clip_id']}:{snr}")
                    noise = rng.choice(noises)
                    offset = rng.randrange(len(noise))
                    mixed = mix_at_snr(speech, fit_length(noise, len(noise), offset), snr)
                    with tempfile.TemporaryDirectory() as td:
                        tmp = Path(td) / "mixed.wav"
                        audio_io.write_wav(tmp, mixed)
                        audio_io.opus_roundtrip(tmp, dst)
            out_clips.append(dict(clip, condition=cond, path=rel, snr=snr))

    with open(root / "clips_aug.jsonl", "w", encoding="utf-8") as f:
        for c in out_clips:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"aug clips: {len(out_clips)} → {root / 'clips_aug.jsonl'}")


if __name__ == "__main__":
    main()
