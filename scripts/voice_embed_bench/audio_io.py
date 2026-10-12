"""音訊 I/O 小工具：stdlib wave 讀寫 16-bit PCM、ffmpeg 轉檔、Opus 往返（模擬 Discord）。"""
from __future__ import annotations

import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

SR = 16000


def read_wav(path) -> tuple[np.ndarray, int]:
    """讀 16-bit PCM wav，回傳 float32 mono（多聲道取平均，值域 [-1,1]）與取樣率。"""
    with wave.open(str(path), "rb") as wf:
        n_ch = wf.getnchannels()
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    return data.astype(np.float32), sr


def write_wav(path, audio: np.ndarray, sr: int = SR) -> None:
    """float → clip [-1,1] → int16 mono 寫出；自動建父目錄。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def ffmpeg_to_wav16k(src, dst) -> None:
    """任意格式 → 16kHz mono s16 wav。"""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(dst)],
        check=True,
    )


def opus_roundtrip(src_wav, dst_wav) -> None:
    """模擬 Discord：48kHz stereo Opus 64k 編碼，再解回 16kHz mono wav。"""
    with tempfile.TemporaryDirectory() as td:
        ogg = Path(td) / "tmp.ogg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src_wav),
             "-ar", "48000", "-ac", "2", "-c:a", "libopus", "-b:a", "64k", str(ogg)],
            check=True,
        )
        ffmpeg_to_wav16k(ogg, dst_wav)
