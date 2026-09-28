"""離線眼驗導聆稿 / 巡禮曲目（docs/PLAN_audiophile_music_tour.md Phase 5），不上台、不入隊。

用法：
  venv_simon/bin/python -m scripts.preview_audiophile_guide --artist 周杰倫 --song 雙截棍
  venv_simon/bin/python -m scripts.preview_audiophile_guide --artist 周杰倫 --album 范特西
    --no-tts       不渲染 TTS（只看稿）
    --fresh        不讀正本快取，強制重查
    --allow-paid   免費額度用完時允許走付費 key（照樣過 PaidUsageGuard 記帳）；預設只用免費

護欄：
  - 只讀正本快取 records/song_knowledge.json 的**暫存複本**，絕不寫正本
    （bot 24/7 同檔整份寫，會互蓋）
  - 預設不花錢：只帶免費 client，付費 client 只在 --allow-paid 才建立
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE, fetch_album_tracklist, fetch_audiophile_guide
from song_knowledge_store import SongKnowledgeStore

LIVE_CACHE_PATH = "records/song_knowledge.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artist", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--song")
    group.add_argument("--album")
    parser.add_argument("--no-tts", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--allow-paid", action="store_true")
    return parser


async def run(args, *, free_client, paid_client, guard, tts_engine, probe_duration,
               live_cache_path, out=print) -> int:
    tmpdir = tempfile.mkdtemp(prefix="audiophile_preview_")
    tmp_path = os.path.join(tmpdir, "song_knowledge.json")
    if not args.fresh and os.path.exists(live_cache_path):
        shutil.copyfile(live_cache_path, tmp_path)
    store = SongKnowledgeStore(path=tmp_path)
    out(f"（快取複本：{tmp_path}，不會寫回正本）")

    if args.album:
        key = f"album_tracklist::{args.artist} - {args.album}"
        hit = bool((store.get(key) or {}).get("tracks"))
        tracks = await fetch_album_tracklist(
            args.artist, args.album, free_client=free_client, paid_client=paid_client,
            guard=guard, store=store,
        )
        if not tracks:
            out(f"❌ 查不到 {args.artist}《{args.album}》可靠的曲目")
            return 1
        out(f"📀 {args.artist}《{args.album}》{'（快取命中）' if hit else ''} 共 {len(tracks)} 首：")
        for i, t in enumerate(tracks, 1):
            out(f"{i}. {t}")
        out(f"來源：{', '.join((store.get(key) or {}).get('sources') or []) or '（無）'}")
        return 0

    label = f"{args.artist} - {args.song}"
    key = f"audiophile::{label}"
    hit = bool((store.get(key) or {}).get("audiophile_guide"))
    text = await fetch_audiophile_guide(
        args.song, args.artist, free_client=free_client, paid_client=paid_client,
        guard=guard, store=store,
    )
    rec = store.get(key) or {}
    is_fallback = text == FALLBACK_GUIDE_TEMPLATE.format(title=args.song)

    out(f"🎧 {label}{'（快取命中）' if hit else ''}")
    out(f"來源：{', '.join(rec.get('sources') or []) or '（無）'}")
    out(f"導聆稿（{len(text)} 字）：{text}")
    if is_fallback:
        out("⚠️ 查不到可靠資料，這是保底台詞（不會寫進快取）")

    if not args.no_tts:
        path = await tts_engine.generate_audio(text)
        if path:
            dur = await probe_duration(path)
            out(f"TTS：{path}（{dur:.1f} 秒）")
        else:
            out("⚠️ TTS 渲染失敗")

    return 1 if is_fallback else 0


def main() -> int:
    args = build_parser().parse_args()
    from google import genai
    from llm_paid import PaidUsageGuard
    from tts_engine import SukiTTS
    from cogs.music_cog_story_arc import MusicStoryArcMixin
    # 鏡像 GeminiRouter 的 key 設定（同 scripts/replay_audio_rescue.py 手法）
    free_key = os.getenv("GOOGLE_API_KEY")
    paid_key = os.getenv("GEMINI_PAID_API_KEY") if args.allow_paid else None
    return asyncio.run(run(
        args,
        free_client=genai.Client(api_key=free_key) if free_key else None,
        paid_client=genai.Client(api_key=paid_key) if paid_key else None,
        guard=PaidUsageGuard(),
        tts_engine=SukiTTS(),
        probe_duration=MusicStoryArcMixin._probe_audio_duration,
        live_cache_path=LIVE_CACHE_PATH,
    ))


if __name__ == "__main__":
    sys.exit(main())
