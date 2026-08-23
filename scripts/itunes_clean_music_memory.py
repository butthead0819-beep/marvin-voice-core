"""用 iTunes Search API 清洗 music_memory.json 裡髒的 YouTube 標題。

Spotify Web API（scripts/spotify_clean_music_memory.py）Development Mode 額度
很緊、一次清洗+除錯就鎖了 ~22.6 小時（2026-08-23 實測）。iTunes Search 免金鑰、
免登入、沒有這種硬鎖，拿來清洗同一份存量資料。

不覆蓋原始 title（保留 provenance），比對成功的寫入新欄位：
itunes_title/itunes_artist/itunes_album/itunes_match_score。跳過已經有
spotify_title（Spotify 那批已清乾淨）或 itunes_checked（這支已查過）的 entry。

小批次執行、可重複呼叫：
    python -m scripts.itunes_clean_music_memory [batch_size]

⚠️ music_memory.json 是 24/7 live bot 的正本，bot 自己也整份記憶體覆蓋存檔
（見 spotify_clean_music_memory.py 同一段教訓：2026-08-23 兩邊同時整份覆蓋互撞，
清洗結果被 bot 的存檔蓋掉）。這支從一開始就用「累積增量、存檔前重新讀最新檔案
只 merge 自己算出的欄位」，不整份覆蓋。
"""
import asyncio
import json
import sys

import itunes_cover
from itunes_cover import _similarity

DB_PATH = "music_memory.json"
DEFAULT_BATCH = 25
SAVE_EVERY = 25
SLEEP_S = 0.15


def _load_data():
    with open(DB_PATH, encoding="utf-8") as f:
        return json.load(f)


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    batch_size = int(args[0]) if args else DEFAULT_BATCH
    dry_run = "--dry-run" in sys.argv

    songs_snapshot = _load_data()["songs"]
    processed = matched = flagged = 0
    pending = {}

    def _flush():
        if dry_run or not pending:
            return
        fresh_data = _load_data()
        fresh_songs = fresh_data["songs"]
        for key, fields in pending.items():
            if key in fresh_songs:
                fresh_songs[key].update(fields)
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(fresh_data, f, ensure_ascii=False, indent=2)
        pending.clear()

    for key, entry in songs_snapshot.items():
        if processed >= batch_size:
            break
        if entry.get("spotify_title") or entry.get("itunes_title") or entry.get("itunes_checked"):
            continue

        title = entry.get("title", "") or ""
        uploader = entry.get("uploader", "") or ""

        meta = await itunes_cover.resolve_metadata(title, uploader)
        processed += 1

        if meta and meta.get("title"):
            score = _similarity(title, meta["title"])
            print(f"[MATCH {score:.2f}] {title!r}")
            print(f"         -> {meta['title']} / {meta.get('artist')} / {meta.get('album')}")
            pending[key] = {
                "itunes_title": meta["title"],
                "itunes_artist": meta.get("artist"),
                "itunes_album": meta.get("album"),
                "itunes_match_score": round(score, 3),
            }
            matched += 1
        else:
            print(f"[NO MATCH] {title!r}")
            pending[key] = {"itunes_checked": True}
            flagged += 1

        if processed % SAVE_EVERY == 0:
            _flush()
            print(f"[checkpoint] merged at processed={processed}")

        await asyncio.sleep(SLEEP_S)

    _flush()
    print(f"\nprocessed={processed} matched={matched} no_match={flagged} dry_run={dry_run}")


if __name__ == "__main__":
    asyncio.run(main())
