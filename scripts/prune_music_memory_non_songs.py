"""掃 music_memory.json 全部歌曲，用 track_quality.is_non_song_video 抓合輯/紀錄片/
長混音帶等非單曲，直接從共用歌曲庫刪除（"每個人的歌單"是這個共用 songs dict 依
requester 過濾出來的視圖，刪掉源頭就對所有人生效）。

時長來源：song["url"]（簽名串流 URL）query string 裡的 `dur=` 參數（yt-dlp 填的，
音樂記憶本身沒存 duration 欄位）；沒有的話退化成只靠標題黑名單判斷。

用法：
  venv_simon/bin/python scripts/prune_music_memory_non_songs.py            # 只印出會刪什麼
  venv_simon/bin/python scripts/prune_music_memory_non_songs.py --apply    # 真的刪 + 先備份
"""
import json
import re
import shutil
import sys
import time

sys.path.insert(0, ".")
from track_quality import is_non_song_video

MUSIC_MEMORY_PATH = "music_memory.json"


def _extract_duration(song: dict) -> float | None:
    url = song.get("url") or ""
    m = re.search(r"[?&]dur=([\d.]+)", url)
    return float(m.group(1)) if m else None


def main():
    apply = "--apply" in sys.argv

    with open(MUSIC_MEMORY_PATH, encoding="utf-8") as f:
        data = json.load(f)
    songs = data.get("songs", {})
    print(f"總歌曲數：{len(songs)}")

    flagged = []
    for key, song in songs.items():
        title = song.get("title", "")
        duration = _extract_duration(song)
        rejected, reason = is_non_song_video(title, duration)
        if rejected:
            flagged.append((key, title, reason))

    print(f"判定非單曲：{len(flagged)}\n")
    for key, title, reason in flagged:
        print(f"  [{reason}] {title[:70]}")

    if not apply:
        print("\n（dry-run，沒有實際刪除。加 --apply 才會真的刪）")
        return

    if not flagged:
        print("\n沒有東西要刪。")
        return

    backup_path = f"{MUSIC_MEMORY_PATH}.bak.{int(time.time())}"
    shutil.copy2(MUSIC_MEMORY_PATH, backup_path)
    print(f"\n已備份到 {backup_path}")

    for key, _title, _reason in flagged:
        songs.pop(key, None)

    with open(MUSIC_MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已刪除 {len(flagged)} 首，剩餘 {len(songs)} 首。")


if __name__ == "__main__":
    main()
