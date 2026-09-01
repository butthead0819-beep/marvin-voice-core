"""把 tests/fixtures/audio_rescue_routing_corpus.jsonl 的句子用 edge-tts 合成成
16kHz mono wav（AudioRescueAgent 吃的格式），檔名帶 index 讓 replay 對得回 expect。

  python scripts/synth_audio_rescue_corpus.py [out_dir]     # 預設 /tmp/audio_rescue_corpus

⚠️ TTS 是乾淨語音，測的是 manifest_description 的意圖分辨力（T7 rubric），不是
「糊字救援」那一面——後者要等 records/rescue_wav/ 累積真 shadow wav。先用這個拿
第一手 routing 訊號。

產物：out_dir/{idx:02d}__{expect}.wav + out_dir/manifest.jsonl（idx→utterance→expect）
接著：python scripts/replay_audio_rescue.py <out_dir> --corpus
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import edge_tts  # noqa: E402

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "audio_rescue_routing_corpus.jsonl"
VOICE = "zh-TW-HsiaoChenNeural"


def _rows():
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield json.loads(line)


async def _synth_one(text: str, out_wav: Path) -> None:
    mp3 = out_wav.with_suffix(".mp3")
    await edge_tts.Communicate(text=text, voice=VOICE).save(str(mp3))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
         "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", str(out_wav)],
        check=True,
    )
    mp3.unlink()


async def _main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = list(_rows())
    manifest = out_dir / "manifest.jsonl"
    with manifest.open("w", encoding="utf-8") as mf:
        for i, r in enumerate(rows):
            safe_expect = r["expect"].replace("__", "-")
            wav = out_dir / f"{i:02d}__{safe_expect}.wav"
            await _synth_one(r["utterance"], wav)
            mf.write(json.dumps(
                {"idx": i, "wav": wav.name, "utterance": r["utterance"],
                 "expect": r["expect"], "note": r["note"]},
                ensure_ascii=False) + "\n")
            print(f"  {wav.name:<44} {r['utterance']}")
    print(f"\n{len(rows)} wav → {out_dir}")
    print(f"next: python scripts/replay_audio_rescue.py {out_dir} --corpus")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/audio_rescue_corpus")
    asyncio.run(_main(target))
