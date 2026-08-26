"""DJ 社交喜好關聯、同歌手連播與時間情境分析模組。

純函式 + 唯讀分析，fail-open：
1. 歌曲與在場成員的社交共鳴（Social Bridge：其他在場者也點過/按過讚/有感觸）
2. 歌手喜好偏好（Artist Affinity：點播者或在場者常聽該歌手）
3. 久別重逢提示（Recency / Long time no play）
4. 同歌手連播（Back-to-Back）偵測
5. 星期與時段環境標籤合成
"""
from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING

from taste_fingerprint import artist_of

if TYPE_CHECKING:
    from music_memory import MusicMemory

_RECENCY_DAYS_THRESHOLD = 30 * 86400  # 超過 30 天未播視為久別重逢
_ARTIST_FREQ_THRESHOLD = 3            # 點過該歌手 >= 3 首視為常客
_ACTIVE_CHAT_WINDOW_S = 180           # 近 3 分鐘
_ACTIVE_CHAT_UTTERANCE_COUNT = 4      # 近 3 分鐘發言 >= 4 次視為熱烈交談


def assess_channel_heat(
    tracker: Any = None,
    conv_buffer: Any = None,
    n_online: int = 0,
    now_ts: float | None = None,
) -> tuple[str, str]:
    """根據在線人數與最近語音發言活躍度，評估聊天室真實熱度並回傳提示詞指令。

    回傳 (mode, instruction):
      - 'solo': 只有 1 人，親密私語
      - 'active_chat': 多人且近 3 分鐘密集發言，走 Live DJ 5秒極簡短打
      - 'quiet_group': 多人但大家安靜（沉浸/工作聽歌），走深度故事與陪伴
      - 'default': 狀態不詳或無人
    """
    if n_online == 1:
        return "solo", "只有一個人在聽，語氣親密一點，像對老朋友說話。"

    now = now_ts if now_ts is not None else time.time()
    recent_cnt = 0

    if tracker is not None:
        window = getattr(tracker, "_window", None)
        if window:
            recent_cnt = sum(1 for e in window if (now - getattr(e, "ts", 0)) <= _ACTIVE_CHAT_WINDOW_S)
    elif conv_buffer is not None:
        try:
            entries = conv_buffer.get_last_n_utterances(6)
            recent_cnt = len(entries)
        except Exception:
            pass

    if n_online >= 2:
        if recent_cnt >= _ACTIVE_CHAT_UTTERANCE_COUNT:
            return "active_chat", "現場聊得很熱烈，說短一點、節奏精簡俐落（5秒內結束），別長篇大論打斷大家講話。"
        return "quiet_group", "大家都在安靜聽歌，可以多帶一點音樂故事、樂器編曲或生活畫面，陪伴感為主。"

    return "default", ""


def detect_back_to_back_artist(prev_title: str, curr_title: str) -> str | None:
    """前後兩首為同一歌手時，回傳歌手名；否則回傳 None。"""
    p_art = artist_of(prev_title).strip()
    c_art = artist_of(curr_title).strip()
    if p_art and c_art and len(p_art) >= 2 and len(c_art) >= 2:
        if p_art.lower() == c_art.lower():
            return c_art
    return None


def format_temporal_atmosphere(
    city: str,
    season: str,
    slot: str,
    ts: float | None = None,
) -> str:
    """合成包含城市、季節、星期與時段的環境字串。"""
    now_dt = datetime.datetime.fromtimestamp(ts if ts is not None else time.time())
    weekday_map = {0: "週一", 1: "週二", 2: "週三", 3: "週四", 4: "週五", 5: "週六", 6: "週日"}
    w_str = weekday_map.get(now_dt.weekday(), "")
    
    parts = []
    if city:
        parts.append(city)
    if season:
        parts.append(season)
    if w_str and slot:
        parts.append(f"{w_str} · {slot}")
    elif w_str or slot:
        parts.append(w_str or slot)

    return f"環境：{' · '.join(parts)}"


def find_song_social_affinity(
    mm: MusicMemory | None,
    info: dict,
    requester: str,
    present_members: set[str] | list[str] | None = None,
    now_ts: float | None = None,
) -> str | None:
    """從 MusicMemory 挖掘當前歌曲或歌手與點播者/在場成員的喜好連結。

    優先度：
    1. 在場其他成員對此歌的按讚/點播/反應（Social Bridge）
    2. 點播者上次聽這首歌已超過 30 天（久別重逢）
    3. 點播者或在場其他成員是該歌手的常客（Artist Affinity）
    """
    if mm is None or not info:
        return None

    members = set(present_members or [])
    now = now_ts if now_ts is not None else time.time()
    songs = mm._data.get("songs", {})
    key = mm._key(info)
    song = songs.get(key)

    # 1. 在場其他成員按讚/點播/感受 (Social Bridge)
    if song and members:
        for m in members:
            if not m or m == requester or "Marvin" in m:
                continue
            # 點播過
            req_cnt = (song.get("requesters") or {}).get(m, 0)
            if req_cnt > 0:
                return f"這首 {requester} 常聽，在場的 {m} 也常聽/點過"
            # 按讚
            if m in (song.get("likes") or {}):
                return f"在場的 {m} 也按過這首歌讚"
            # 記錄過感受
            rx = (song.get("reactions") or {}).get(m, {})
            feelings = rx.get("feelings", [])
            if feelings:
                return f"在場的 {m} 曾記錄對這首歌的感受：{' / '.join(feelings[:2])}"

    # 2. 久別重逢（Recency）
    if song and requester:
        plays = [p for p in song.get("plays", []) if p.get("by") == requester]
        if plays:
            last_play = plays[-1].get("ts")
            if isinstance(last_play, (int, float)) and (now - last_play) >= _RECENCY_DAYS_THRESHOLD:
                return f"距 {requester} 上次點這首已經過了一陣子"

    # 3. 歌手偏好 (Artist Affinity)
    title = info.get("title", "")
    target_artist = artist_of(title).strip()
    if target_artist and len(target_artist) >= 2:
        # 計算點播者與在場成員對此歌手的累積點播次數
        artist_counts: dict[str, int] = {}
        for s in songs.values():
            s_title = s.get("title", "")
            s_artist = artist_of(s_title).strip()
            if s_artist and s_artist.lower() == target_artist.lower():
                for u, c in (s.get("requesters") or {}).items():
                    if u and "Marvin" not in u:
                        artist_counts[u] = artist_counts.get(u, 0) + c

        if requester and artist_counts.get(requester, 0) >= _ARTIST_FREQ_THRESHOLD:
            return f"{requester} 常點 {target_artist} 的歌"

        for m in members:
            if m and m != requester and "Marvin" not in m:
                if artist_counts.get(m, 0) >= _ARTIST_FREQ_THRESHOLD:
                    return f"在場的 {m} 也是 {target_artist} 的常客"

    return None
