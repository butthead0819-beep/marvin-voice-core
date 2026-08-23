"""Phase 1 手動驗收：Spotify Connect API 能否遠端操控 Mac 上的官方 Spotify app。

用法：
    python scripts/spotify_connect_smoke_test.py status   # 讀目前播放狀態
    python scripts/spotify_connect_smoke_test.py queue "歌名 歌手"  # 搜尋+排進 queue
    python scripts/spotify_connect_smoke_test.py next     # 跳下一首

第一次執行會開瀏覽器要求登入授權（Authorization Code flow），
token 快取在 .spotify_connect_cache（已加 .gitignore）。
需要 Mac 上的 Spotify app 開著且正在播放（Connect 找不到 idle 裝置的部分行為會不同）。
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

import spotipy
from spotipy.oauth2 import SpotifyOAuth

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-modify-playback-state user-read-playback-state"
CACHE_PATH = ".spotify_connect_cache"


def get_client():
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_path=CACHE_PATH,
    )
    return spotipy.Spotify(auth_manager=auth)


def cmd_status(sp):
    playback = sp.current_playback()
    if not playback or not playback.get("item"):
        print("目前沒有偵測到播放中的裝置/歌曲")
        return
    item = playback["item"]
    progress_ms = playback["progress_ms"]
    duration_ms = item["duration_ms"]
    device = playback["device"]["name"]
    artists = ", ".join(a["name"] for a in item["artists"])
    print(f"裝置: {device}")
    print(f"歌曲: {item['name']} - {artists}")
    print(f"進度: {progress_ms/1000:.1f}s / {duration_ms/1000:.1f}s")
    print(f"is_playing: {playback['is_playing']}")


def cmd_queue(sp, query):
    results = sp.search(q=query, type="track", limit=1)
    tracks = results["tracks"]["items"]
    if not tracks:
        print(f"搜不到: {query}")
        return
    track = tracks[0]
    artists = ", ".join(a["name"] for a in track["artists"])
    sp.add_to_queue(track["uri"])
    print(f"已排進 queue: {track['name']} - {artists} ({track['uri']})")


def cmd_next(sp):
    sp.next_track()
    print("已送出換下一首指令")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1]
    sp = get_client()

    if action == "status":
        cmd_status(sp)
    elif action == "queue":
        if len(sys.argv) < 3:
            print("用法: python scripts/spotify_connect_smoke_test.py queue \"歌名 歌手\"")
            sys.exit(1)
        cmd_queue(sp, sys.argv[2])
    elif action == "next":
        cmd_next(sp)
    else:
        print(f"未知指令: {action}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
