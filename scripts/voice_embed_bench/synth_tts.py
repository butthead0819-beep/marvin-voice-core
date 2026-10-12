"""用 edge-tts 合成測試語音（clean 條件），輸出 16kHz mono wav，可續跑。

CLI：python -m scripts.voice_embed_bench.synth_tts [--corpus ...] [--root ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import tempfile
from pathlib import Path

from scripts.voice_embed_bench import audio_io

DEFAULT_VOICES = (
    "zh-TW-HsiaoChenNeural", "zh-TW-YunJheNeural", "zh-TW-HsiaoYuNeural",
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural",
)
RATES = ("-10%", "+0%", "+15%")
PITCHES = ("-5Hz", "+0Hz", "+5Hz")


def plan_clips(items: list[dict], voices: tuple[str, ...], seed: int) -> list[dict]:
    """純函式：每個 item × 每個 voice 一筆，rate/pitch 由 seed 決定。"""
    clips = []
    for item in items:
        for voice in voices:
            rng = random.Random(f"{seed}:{item['id']}:{voice}")
            clip_id = f"{item['id']}__{voice}"
            clips.append({
                "clip_id": clip_id,
                "item_id": item["id"],
                "text": item["text"],
                "label": item["label"],
                "kind": item["kind"],
                "voice": voice,
                "rate": rng.choice(RATES),
                "pitch": rng.choice(PITCHES),
                "condition": "clean",
                "path": f"audio/clean/{clip_id}.wav",
            })
    return clips


async def synth_all(clips: list[dict], root: Path, concurrency: int = 4) -> None:
    import edge_tts  # lazy：重依賴

    root = Path(root)
    sem = asyncio.Semaphore(concurrency)

    async def one(clip: dict) -> None:
        dst = root / clip["path"]
        if dst.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        async with sem:
            for attempt in range(3):
                try:
                    with tempfile.TemporaryDirectory() as td:
                        mp3 = Path(td) / "tmp.mp3"
                        await edge_tts.Communicate(
                            clip["text"], clip["voice"],
                            rate=clip["rate"], pitch=clip["pitch"],
                        ).save(str(mp3))
                        audio_io.ffmpeg_to_wav16k(mp3, dst)
                    return
                except Exception as e:  # noqa: BLE001 — 單筆失敗不中斷整批
                    if attempt == 2:
                        print(f"warning: 合成失敗，跳過 {clip['clip_id']}: {e}")
                        dst.unlink(missing_ok=True)
                        return
                    await asyncio.sleep(2 ** attempt)

    await asyncio.gather(*(one(c) for c in clips))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default="records/voice_embed_bench/corpus.jsonl")
    ap.add_argument("--root", default="records/voice_embed_bench")
    ap.add_argument("--voices", default=",".join(DEFAULT_VOICES))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    with open(args.corpus, encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]
    voices = tuple(v for v in args.voices.split(",") if v)
    root = Path(args.root)
    clips = plan_clips(items, voices, args.seed)
    asyncio.run(synth_all(clips, root, args.concurrency))

    ok = [c for c in clips if (root / c["path"]).exists()]
    with open(root / "clips_clean.jsonl", "w", encoding="utf-8") as f:
        for c in ok:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"clips: {len(ok)}/{len(clips)} → {root / 'clips_clean.jsonl'}")


if __name__ == "__main__":
    main()
