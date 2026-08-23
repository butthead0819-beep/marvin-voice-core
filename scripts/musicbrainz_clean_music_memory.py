"""用 MusicBrainz Search API 清洗 music_memory.json 裡髒的 YouTube 標題。

背景：Spotify Web API（scripts/spotify_clean_music_memory.py）Development Mode
額度很緊，一次清洗+除錯就鎖了 ~22.6 小時（2026-08-23 實測）；iTunes Search 對中文
歌常回英文翻譯標題、甚至配錯歌（cross-language rescue 是為封面圖設計的寬鬆容錯，
不適合當「乾淨標題」資料源，見 itunes_cover.py 文件）。MusicBrainz 免金鑰、
社群維護的音樂資料庫，回傳原文標題（不翻譯），拿來當第三個資料源清洗同一份存量。

不覆蓋原始 title（保留 provenance），比對成功的寫入新欄位：
mb_title/mb_artist/mb_album/mb_match_score。跳過已經有 spotify_title/itunes_title
（其他資料源已清乾淨）或 mb_checked（這支已查過）的 entry。

⚠️ MusicBrainz 速率限制嚴格（實測 <1.2s 間隔會噴 503，比文件寫的 1 req/s 更敏感）
——每次查詢間隔固定 sleep，503 用退避重試，別調快。

比對策略（field-scoped 優先，複用 spotify_query_build.py 已驗證的候選抽取邏輯，
別重造）：
  ① 用 build_field_queries() 抽出的第一個乾淨候選（通常是第一段 CJK 語意塊）查
     `recording:"..."`；MB 用 Lucene 語法，未清洗的標題常含 `:` `(` `)` 等會讓
     查詢失敗或誤配（實測撞過「(特別演出: 派偉俊)」這種未列入 cruft 清單的括號
     被誤判成主標題）。
  ② field 查詢空/低信心 → 退回自由文字（cleaned_title + artist），比對回傳的
     artist-credit 跟我方 artist 字串的相似度做信心門檻，擋錯歌/錯藝人。

小批次執行、可重複呼叫：
    python -m scripts.musicbrainz_clean_music_memory [batch_size] [--dry-run]
"""
import json
import sys
import time

import requests

from song_name_clean import clean_title_regex
from itunes_cover import _clean_artist, _similarity
from spotify_query_build import build_field_queries

MB_URL = "https://musicbrainz.org/ws/2/recording/"
HEADERS = {"User-Agent": "MarvinDiscordBot/1.0 (contact: butthead0819@gmail.com)"}
DB_PATH = "music_memory.json"
DEFAULT_BATCH = 25
SAVE_EVERY = 25
SLEEP_S = 1.3
FIELD_SANITY_THRESHOLD = 0.3
FREE_TEXT_THRESHOLD = 0.45
MIN_ARTIST_SIM = 0.25
MAX_RETRIES = 3


def _load_data():
    with open(DB_PATH, encoding="utf-8") as f:
        return json.load(f)


def _search(query, limit=5):
    for attempt in range(MAX_RETRIES):
        r = requests.get(MB_URL, params={"query": query, "fmt": "json", "limit": limit},
                          headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json().get("recordings") or []
        if r.status_code == 503:
            time.sleep(SLEEP_S * (attempt + 2))
            continue
        return []
    return []


def _pick_best(recordings, track_hint, artist_full):
    """⚠️ 曲名很容易撞名（翻奏/鋼琴版/不同歌手同名曲——實測撞過「我們很好」配到
    林峯、「黑色毛衣」配到王喆鋼琴翻奏專輯），標題分數高不能蓋過藝人不符——
    有 artist_full 時，artist_sim 沒過 MIN_ARTIST_SIM 硬門檻直接刷掉候選，不能
    用 max(title_sim, avg) 這種讓強標題分數矇混過關的算法。
    """
    best_score, best = 0.0, None
    for rec in recordings:
        rec_artist = ", ".join(a["name"] for a in rec.get("artist-credit", []))
        title_sim = _similarity(track_hint, rec.get("title", ""))
        if artist_full:
            artist_sim = _similarity(artist_full, rec_artist)
            if artist_sim < MIN_ARTIST_SIM:
                continue
            score = (title_sim + artist_sim) / 2
        else:
            score = title_sim
        if score > best_score:
            best_score, best = score, rec
    return best, best_score


def match_track(cleaned_title, artist_full):
    for q, track_candidate in build_field_queries(cleaned_title, artist_full):
        mb_query = f'recording:"{track_candidate}"'
        recs = _search(mb_query)
        time.sleep(SLEEP_S)
        if not recs:
            continue
        best, score = _pick_best(recs, track_candidate, artist_full)
        if best and score >= FIELD_SANITY_THRESHOLD:
            return best, score, f"field:{track_candidate}"

    query = f"{artist_full} {cleaned_title}".strip() if artist_full else cleaned_title
    recs = _search(query)
    time.sleep(SLEEP_S)
    if not recs:
        return None
    best, score = _pick_best(recs, cleaned_title, artist_full)
    if best and score >= FREE_TEXT_THRESHOLD:
        return best, score, "free_text"
    return None


def main():
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
        if (entry.get("spotify_title") or entry.get("itunes_title")
                or entry.get("mb_title") or entry.get("mb_checked")):
            continue

        title = entry.get("title", "") or ""
        uploader = entry.get("uploader", "") or ""
        cleaned_title = clean_title_regex(title) or title
        artist_full = _clean_artist(uploader) or ""

        result = match_track(cleaned_title, artist_full)
        processed += 1

        if result:
            rec, score, via = result
            rec_artist = ", ".join(a["name"] for a in rec.get("artist-credit", []))
            album = (rec.get("releases") or [{}])[0].get("title")
            print(f"[MATCH {score:.2f} via={via}] {title!r}")
            print(f"         -> {rec['title']} / {rec_artist} / {album}")
            pending[key] = {
                "mb_title": rec["title"],
                "mb_artist": rec_artist,
                "mb_album": album,
                "mb_match_score": round(score, 3),
            }
            matched += 1
        else:
            print(f"[NO MATCH] {title!r} (cleaned: {cleaned_title!r} / artist: {artist_full!r})")
            pending[key] = {"mb_checked": True}
            flagged += 1

        if processed % SAVE_EVERY == 0:
            _flush()
            print(f"[checkpoint] merged at processed={processed}")

    _flush()
    print(f"\nprocessed={processed} matched={matched} no_match={flagged} dry_run={dry_run}")


if __name__ == "__main__":
    main()
