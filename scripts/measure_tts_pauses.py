"""
docs/tts_breath_plan.md Phase 2：量測不同標點在 edge-tts 輸出裡實際產生的停頓長度。

直接送原始字串（不經 SukiTTS._clean_text），目的是量「標點本身」的效果。
需要連網（呼叫 edge-tts），在本機跑，不進 CI。
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

VOICE = "zh-TW-YunJheNeural"
RATE = "-20%"
PITCH = "-15Hz"

FRAMES = [
    "我想想{P}好吧",
    "這首歌{P}其實是他二十歲寫的",
    "欸{P}你有沒有聽到",
]

VARIANTS = ["", "，", ",", "。", ".", "。。。", "。。。。。。", " "]

_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


def parse_silences(stderr: str, total_dur: float) -> list[float]:
    """從 ffmpeg silencedetect stderr 解析句中（排除開頭/結尾）靜音段的長度。"""
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(stderr)]
    ends = [(float(m.group(1)), float(m.group(2))) for m in _SILENCE_END_RE.finditer(stderr)]

    durations = []
    for start, (end, duration) in zip(starts, ends):
        if start <= 0.05:
            continue
        if end >= total_dur - 0.05:
            continue
        durations.append(duration)
    return durations


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _detect_silences(path: Path) -> str:
    out = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "silencedetect=noise=-40dB:d=0.08", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return out.stderr


async def _synthesize(text: str, out_path: Path) -> None:
    import edge_tts

    # edge 約每 16~20 次會隨機回 NoAudioReceived（同句重送就好），重試 3 次
    for attempt in range(3):
        try:
            comm = edge_tts.Communicate(text, voice=VOICE, rate=RATE, pitch=PITCH)
            with open(out_path, "wb") as f:
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
            return
        except edge_tts.exceptions.NoAudioReceived:
            if attempt == 2:
                raise


def main() -> None:
    import asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="./tts_pause_out")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for frame_idx, frame in enumerate(FRAMES):
        for variant_idx, variant in enumerate(VARIANTS):
            text = frame.format(P=variant)
            mp3_path = out_dir / f"{frame_idx}_{variant_idx}.mp3"
            asyncio.run(_synthesize(text, mp3_path))

            total_dur = _ffprobe_duration(mp3_path)
            stderr = _detect_silences(mp3_path)
            mid_pauses = parse_silences(stderr, total_dur)

            rows.append((frame, repr(variant), total_dur, mid_pauses))

    print("| 句框 | 變體 | 總長(s) | 句中停頓(s) |")
    print("|---|---|---|---|")
    for frame, variant, total_dur, mid_pauses in rows:
        pauses_str = ", ".join(f"{d:.3f}" for d in mid_pauses) if mid_pauses else "-"
        print(f"| {frame} | {variant} | {total_dur:.3f} | {pauses_str} |")


if __name__ == "__main__":
    main()
