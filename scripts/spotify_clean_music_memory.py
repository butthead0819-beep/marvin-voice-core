"""用 Spotify Search API 清洗 music_memory.json 裡髒的 YouTube 標題。

不覆蓋原始 title（保留 provenance），比對成功的寫入新欄位：
spotify_title/spotify_artist/spotify_album/spotify_uri/spotify_match_score。
查不到/信心不足的標記 spotify_checked=True（下次跳過，避免重複打 API）。

小批次執行、可重複呼叫（已處理過的 entry 自動跳過）：
    python -m scripts.spotify_clean_music_memory [batch_size] [--dry-run]

比對策略（兩層，field-scoped 優先）：
  ① Spotify 支援 `track:`/`artist:` 欄位限定搜尋，比自由文字精準一個量級，但
     對「中英雙語合併」字串很敏感（"track:我們很好 Better Days" 查不到，
     "track:我們很好" 才秒配到；"artist:林俊傑 JJ Lin" 查不到，
     "artist:JJ Lin" 才配到）。所以中/英各自拆開、剝掉標題開頭重複的 artist
     前綴，試幾種組合。field 查詢一有結果直接採用（雙欄位已限定，不需要再比相似度）。
  ② field 查詢全落空才退回自由文字＋相似度門檻（複用 itunes_cover.py 已驗證的
     clean_title_regex/_similarity/threshold pattern，別重造）。
"""

import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from song_name_clean import clean_title_regex
from itunes_cover import _clean_artist, _norm, _similarity
from spotify_query_build import build_field_queries

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-modify-playback-state user-read-playback-state"
CACHE_PATH = ".spotify_connect_cache"
DB_PATH = "music_memory.json"
THRESHOLD = 0.55
FIELD_SANITY_THRESHOLD = 0.3
DEFAULT_BATCH = 5
SLEEP_S = 0.25
SAVE_EVERY = 25


def get_client():
    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_path=CACHE_PATH,
    )
    return spotipy.Spotify(auth_manager=auth)


def match_field_scoped(sp, cleaned_title, artist_full):
    """field-scoped 查詢精準，但短/模糊的 track candidate（如雜訊誤判成的兩字母
    token）偶爾會讓 Spotify 配到完全不相關的曲目（實測撞過巴哈管弦組曲）。field
    query 本身不做相似度篩選、卻可能誤配，所以命中後仍要拿查詢用的 track candidate
    跟回傳的 track 名比對一次相似度守門，擋掉這種「query 語法對但語意不對」的假陽性。
    """
    for q, track_candidate in build_field_queries(cleaned_title, artist_full):
        results = sp.search(q=q, type="track", limit=1)
        tracks = results["tracks"]["items"]
        if tracks:
            track = tracks[0]
            sim = _similarity(track_candidate, track["name"])
            if sim >= FIELD_SANITY_THRESHOLD:
                return track, q, sim
        time.sleep(SLEEP_S)
    return None, None, None


def match_free_text(sp, cleaned_title, artist_full):
    query = f"{artist_full} {cleaned_title}".strip() if artist_full else cleaned_title
    results = sp.search(q=query, type="track", limit=5)
    tracks = results["tracks"]["items"]
    if not tracks:
        return None

    ncleaned = _norm(cleaned_title)
    best_score, best = 0.0, None
    for t in tracks:
        t_artist = ", ".join(a["name"] for a in t["artists"])
        cand = f"{t_artist} {t['name']}".strip()
        score = max(_similarity(query, cand), _similarity(cleaned_title, t["name"]))
        ntrack = _norm(t["name"])
        if ntrack and (ncleaned in ntrack or ntrack in ncleaned):
            score = max(score, 0.7)
        if score > best_score:
            best_score, best = score, t

    if best_score >= THRESHOLD:
        return best, best_score
    return None


def match_track(sp, cleaned_title, artist_full):
    track, via_query, sim = match_field_scoped(sp, cleaned_title, artist_full)
    if track:
        return track, sim, f"field:{via_query}"

    result = match_free_text(sp, cleaned_title, artist_full)
    if result:
        track, score = result
        return track, score, "free_text"
    return None


def _load_data():
    with open(DB_PATH, encoding="utf-8") as f:
        return json.load(f)


def main():
    """⚠️ music_memory.json 是 24/7 live bot 的正本，bot 自己也持有整份記憶體副本、
    每次 record_play/toggle_like 就整份覆蓋存檔。這支腳本絕不能比照辦理（讀一次、
    跑很久、整份記憶體寫回去）——那樣兩邊互相覆蓋，誰後存誰贏，會把 bot 同時間寫入
    的真實使用者資料（或反過來，這支腳本自己的清洗結果）蓋掉（2026-08-23 實測發生
    過：背景跑到一半、bot 錄了幾筆新播放並存檔，把先前已寫入的 spotify_* 欄位全部
    蓋掉）。改成只累積「這輪算出的欄位增量」，存檔時重新讀最新檔案、只把增量
    merge 進對應 key，其餘欄位（包含 bot 同時間寫入的任何東西）完全不碰。
    """
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    batch_size = int(args[0]) if args else DEFAULT_BATCH
    dry_run = "--dry-run" in sys.argv

    songs_snapshot = _load_data()["songs"]
    sp = get_client()
    processed = matched = flagged = 0
    pending = {}  # key -> {field: value, ...} 待 merge 的增量

    def _flush():
        if dry_run or not pending:
            return
        fresh_data = _load_data()
        fresh_songs = fresh_data["songs"]
        for key, fields in pending.items():
            if key in fresh_songs:  # 歌可能在這期間被 bot 端刪掉（如 undo_play）→ 略過
                fresh_songs[key].update(fields)
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(fresh_data, f, ensure_ascii=False, indent=2)
        pending.clear()

    for key, entry in songs_snapshot.items():
        if processed >= batch_size:
            break
        if "spotify_title" in entry or entry.get("spotify_checked"):
            continue

        title = entry.get("title", "") or ""
        uploader = entry.get("uploader", "") or ""
        cleaned_title = clean_title_regex(title) or title
        artist_full = _clean_artist(uploader) or ""

        result = match_track(sp, cleaned_title, artist_full)
        processed += 1

        if result:
            track, score, via = result
            t_artist = ", ".join(a["name"] for a in track["artists"])
            print(f"[MATCH {score:.2f} via={via}] {title!r}")
            print(f"         -> {track['name']} / {t_artist} / {track['album']['name']}")
            pending[key] = {
                "spotify_title": track["name"],
                "spotify_artist": t_artist,
                "spotify_album": track["album"]["name"],
                "spotify_uri": track["uri"],
                "spotify_match_score": round(score, 3),
            }
            matched += 1
        else:
            print(f"[NO MATCH] {title!r} (cleaned: {cleaned_title!r} / artist: {artist_full!r})")
            pending[key] = {"spotify_checked": True}
            flagged += 1

        if processed % SAVE_EVERY == 0:
            _flush()
            print(f"[checkpoint] merged at processed={processed}")

        time.sleep(SLEEP_S)

    _flush()
    print(f"\nprocessed={processed} matched={matched} no_match={flagged} dry_run={dry_run}")


if __name__ == "__main__":
    main()
