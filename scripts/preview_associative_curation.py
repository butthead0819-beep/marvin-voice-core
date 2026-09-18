#!/usr/bin/env python3
"""preview_associative_curation.py — 離線預覽對話關聯性與歌詞金句選曲。

讀取 daily log 的 STT 紀錄，切片提取真實對話，呼叫 associative_curation 產生推薦與 DJ 串場。
使用範例：
    python3 scripts/preview_associative_curation.py
    python3 scripts/preview_associative_curation.py --file records/daily/2026-09-17.log --sample-idx 0
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# 確保根目錄可 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from associative_curation import curate_associative_song
from llm_pool import call_paid_review


def extract_dialogue_clusters(log_path: str, min_utts: int = 3) -> list[list[dict]]:
    """從 STT daily log 抽取連續對話叢集。"""
    p = Path(log_path)
    if not p.exists():
        print(f"❌ 找不到 log 檔案: {log_path}")
        return []

    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    line_re = re.compile(r"^[\d\-:,\s]+ - \[(?P<speaker>[^\]]+)\] (?:\(Debounced\) )?(?P<text>.*)$")

    clusters: list[list[dict]] = []
    current_cluster: list[dict] = []

    for line in lines:
        m = line_re.match(line)
        if not m:
            continue
        speaker = m.group("speaker").strip()
        text = m.group("text").strip()

        # 排除系統/BOT 說話
        if speaker in ("BOT", "系統撤離", "串流結束", "⚡喚醒", "✅Query通過") or speaker.startswith("BOT"):
            if len(current_cluster) >= min_utts:
                clusters.append(current_cluster)
            current_cluster = []
            continue

        if text and len(text) > 1:
            current_cluster.append({"speaker": speaker, "text": text})

    if len(current_cluster) >= min_utts:
        clusters.append(current_cluster)

    return clusters


async def main():
    parser = argparse.ArgumentParser(description="預覽對話關聯性選曲")
    parser.add_argument("--file", default="records/daily/2026-09-17.log", help="STT 日誌路徑")
    parser.add_argument("--keyword", default=None, help="搜尋對話中包含此關鍵字的話題切片（例如：鑰匙、喝、啤酒）")
    parser.add_argument("--cluster-idx", type=int, default=None, help="指定第幾個對話叢集")
    args = parser.parse_args()

    clusters = extract_dialogue_clusters(args.file)
    if not clusters:
        print("未找到有效對話叢集。")
        return

    print(f"📊 從 {args.file} 提取到 {len(clusters)} 個對話叢集\n")

    test_slices: list[tuple[str, list[dict]]] = []

    if args.keyword:
        for ci, c in enumerate(clusters):
            for ui, u in enumerate(c):
                if args.keyword in u["text"]:
                    start = max(0, ui - 3)
                    end = min(len(c), ui + 8)
                    slice_label = f"叢集 {ci} (命中關鍵字『{args.keyword}』)"
                    test_slices.append((slice_label, c[start:end]))
                    break
            if test_slices:
                break
    elif args.cluster_idx is not None:
        c = clusters[args.cluster_idx]
        test_slices.append((f"指定叢集 {args.cluster_idx}", c[-12:]))
    else:
        # 預設展示昨日精選三個經典場景
        keywords = ["鑰匙", "今天要不要喝", "冰的啤酒"]
        for kw in keywords:
            found = False
            for ci, c in enumerate(clusters):
                for ui, u in enumerate(c):
                    if kw in u["text"]:
                        start = max(0, ui - 3)
                        end = min(len(c), ui + 8)
                        test_slices.append((f"場景：『{kw}』(叢集 {ci})", c[start:end]))
                        found = True
                        break
                if found:
                    break

    core_artists = ["美秀集團", "茄子蛋", "滅火器", "草東沒有派對", "告五人", "伍佰", "周杰倫", "五月天"]
    exclude_titles = ["大風吹", "愛人錯過"]

    for label, utts in test_slices:
        print(f"====================【{label}】====================")
        for u in utts:
            print(f"  {u['speaker']}: {u['text']}")
        print("-------------------------------------------------------")
        print("🧠 正在呼叫 LLM 進行對話場景與歌詞金句關聯選曲...")

        members = list({u["speaker"] for u in utts})
        pick = await curate_associative_song(
            utts,
            core_artists=core_artists,
            exclude_titles=exclude_titles,
            members=members,
            call_fn=call_paid_review,
        )

        if pick:
            print("\n🎯【選曲成果】")
            print(f"  • 觀察場景/痛點: {pick.observed_topic}")
            print(f"  • 核心歌詞金句: {pick.target_lyric}")
            print(f"  • 推薦曲目:     {pick.artist} - 《{pick.song}》")
            print(f"  • 推薦理由:     {pick.reason}")
            print(f"  • DJ 串場台詞:  「{pick.dj_line}」（字數: {len(pick.dj_line)} 字）\n")
        else:
            print("❌ 未產出有效推薦。\n")


if __name__ == "__main__":
    asyncio.run(main())
