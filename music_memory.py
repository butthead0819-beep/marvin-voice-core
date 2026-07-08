"""
🎵 MusicMemory — 個人音樂記憶系統
追蹤點播記錄、情感反應、歌詞共鳴與推薦回饋。
"""
import json
import os
import re
import time
import datetime
import logging

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/watch\?v=)([A-Za-z0-9_-]{11})")


def extract_video_id(url: str) -> str | None:
    """從 YouTube watch / youtu.be URL 抽出穩定的 11 碼 videoId；抽不到回 None。

    用途：自動點播排除改用 videoId（穩定）取代歌名（yt-dlp 每次解析會變）。
    """
    if not url:
        return None
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None

# 5/18 incident — 原本 apply_stt_correction 內 lazy `from rapidfuzz import fuzz`
# 在 async hot path 跟 ThreadPoolExecutor 第三方 thread 同時 import 同個 module
# 觸發 macOS Python import lock deadlock (Errno 11 EDEADLK)，user 點歌
# silent fail。改成 module top-level import 確保 bot 啟動就裝載完成，
# runtime 不再有 import race。
try:
    from rapidfuzz import fuzz as _rapidfuzz_fuzz
except ImportError:
    _rapidfuzz_fuzz = None  # optional dependency: 沒裝就跳過模糊比對

logger = logging.getLogger(__name__)


def rename_user_in_songs(songs: dict, old: str, new: str) -> None:
    """Rename a user across all per-song sub-structures（in-place）。

    用途：alias merge，例如「狗與鹿」→「狗與露」。
      - requesters: 數值加總後刪舊 key
      - plays[].by: 直接 rename
      - reactions: feelings/quotes union；其他子 key target wins
      - connections: 換掉並 dedup
    No-op 安全：舊 key 不存在不會炸。
    """
    if not old or not new or old == new:
        return
    for s in songs.values():
        # requesters: sum
        req = s.get("requesters") or {}
        if old in req:
            req[new] = req.get(new, 0) + req.pop(old)
        # plays.by: rename
        for p in s.get("plays") or []:
            if p.get("by") == old:
                p["by"] = new
        # reactions: merge per-user
        rx = s.get("reactions") or {}
        if old in rx:
            src = rx.pop(old)
            if new in rx:
                dst = rx[new]
                # feelings union（保留 dst 順序 + 加 src 新項）
                df = list(dst.get("feelings") or [])
                for x in (src.get("feelings") or []):
                    if x not in df:
                        df.append(x)
                dst["feelings"] = df[:10]
                # quotes 同樣 union
                dq = list(dst.get("quotes") or [])
                for x in (src.get("quotes") or []):
                    if x and x not in dq:
                        dq.append(x)
                dst["quotes"] = dq[-5:]
                # lyric_match：dst 缺才補
                if src.get("lyric_match") and not dst.get("lyric_match"):
                    dst["lyric_match"] = src["lyric_match"]
            else:
                rx[new] = src
        # connections: 換 + dedup
        conn = s.get("connections") or []
        new_conn = []
        seen = set()
        for u in conn:
            mapped = new if u == old else u
            if mapped not in seen:
                new_conn.append(mapped)
                seen.add(mapped)
        s["connections"] = new_conn


class MusicMemory:

    def __init__(self, path: str = "music_memory.json"):
        self.path = path
        self._data = self._load()

    # ── Persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {"songs": {}, "recommendations": {}}
        before = len(data.get("songs", {}))
        self._migrate_songs_keys(data)
        after = len(data.get("songs", {}))
        if before != after:
            # 立刻持久化合併結果，避免 bot 此進程內後續 _save 用舊 in-memory 覆蓋
            self._data = data
            self._save()
        return data

    def _migrate_songs_keys(self, data: dict) -> None:
        """把含 expire 的 yt-dlp stream URL key 改成穩定的 webpage_url key，
        並合併同首歌的多份髒 entry。in-place，無 webpage_url 的舊 entry 不動。"""
        songs = data.get("songs")
        if not songs:
            return
        new_songs: dict = {}
        merged_count = 0
        for old_key, s in songs.items():
            wp = s.get("webpage_url")
            target_key = wp if wp else old_key
            if target_key not in new_songs:
                new_songs[target_key] = s
                continue
            # 同 webpage_url 已存在 → 合併
            self._merge_song_into(new_songs[target_key], s)
            merged_count += 1
        if merged_count > 0:
            logger.info(f"🔧 [MusicMemory] 遷移合併 {merged_count} 份髒 entry → "
                        f"{len(songs)} 筆變 {len(new_songs)} 筆")
        data["songs"] = new_songs

    @staticmethod
    def _merge_song_into(dst: dict, src: dict) -> None:
        """合併 src 到 dst：plays / requesters / reactions / connections 並集；
        title/uploader 留 dst（任一份都行，都是同一首歌）。"""
        dst["total_plays"] = dst.get("total_plays", 0) + src.get("total_plays", 0)
        dst["plays"] = (dst.get("plays", []) + src.get("plays", []))[-50:]
        req_dst = dst.setdefault("requesters", {})
        for u, n in src.get("requesters", {}).items():
            req_dst[u] = req_dst.get(u, 0) + n
        rx_dst = dst.setdefault("reactions", {})
        for u, r in src.get("reactions", {}).items():
            if u not in rx_dst:
                rx_dst[u] = r
                continue
            ex = rx_dst[u]
            ex["feelings"] = list(dict.fromkeys(ex.get("feelings", []) + r.get("feelings", [])))[:10]
            quotes = ex.get("quotes", [])
            for q in r.get("quotes", []):
                if q and q not in quotes:
                    quotes.append(q)
            ex["quotes"] = quotes[-5:]
            if r.get("lyric_match") and not ex.get("lyric_match"):
                ex["lyric_match"] = r["lyric_match"]
        conn_dst = dst.setdefault("connections", [])
        for c in src.get("connections", []):
            if c not in conn_dst:
                conn_dst.append(c)
        likes_dst = dst.setdefault("likes", {})
        for u, ts in src.get("likes", {}).items():
            likes_dst[u] = max(likes_dst.get(u, 0), ts)   # 並集、較新 ts 勝

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"❌ [MusicMemory] 儲存失敗: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────

    def _key(self, info: dict) -> str:
        return (
            info.get("webpage_url")
            or info.get("url")
            or f"{info.get('title', '')}|{info.get('uploader', '')}"
        )

    def time_slot(self, ts: float) -> str:
        h = datetime.datetime.fromtimestamp(ts).hour
        if h < 5:  return "凌晨"
        if h < 9:  return "早晨"
        if h < 12: return "上午"
        if h < 18: return "下午"
        if h < 21: return "傍晚"
        return "深夜"

    # ── Write ─────────────────────────────────────────────────────────────

    def record_play(self, info: dict, requested_by: str):
        key = self._key(info)
        songs = self._data.setdefault("songs", {})
        if key not in songs:
            songs[key] = {
                "title": info.get("title", ""),
                "uploader": info.get("uploader", ""),
                "url": info.get("url", ""),
                "webpage_url": info.get("webpage_url", ""),
                "total_plays": 0,
                "plays": [],
                "requesters": {},
                "reactions": {},
                "connections": [],
            }
        s = songs[key]
        ts = time.time()
        s["total_plays"] = s.get("total_plays", 0) + 1
        s["plays"].append({
            "by": requested_by,
            "ts": ts,
            "time_slot": self.time_slot(ts),
            "date": datetime.datetime.now().strftime("%Y-%m-%d"),
        })
        s["plays"] = s["plays"][-50:]
        s.setdefault("requesters", {})[requested_by] = s["requesters"].get(requested_by, 0) + 1
        self._save()

    def toggle_like(self, info: dict, username: str) -> bool | None:
        """按讚/取消讚一首歌。回傳新狀態（True=已讚 / False=取消）；歌不存在（沒播過）回 None。

        likes 是明確的正向訊號（平行 requesters），餵 build_member_pools 讓喜好擴散到多人
        （次於點播者計分）。一人一首一讚、可 toggle。歌只能在播過（已 record_play）後被讚。
        """
        if not username:
            return None
        key = self._key(info)
        songs = self._data.get("songs", {})
        if key not in songs:
            return None
        likes = songs[key].setdefault("likes", {})
        if username in likes:
            del likes[username]
            new_state = False
        else:
            likes[username] = time.time()
            new_state = True
        self._save()
        return new_state

    def get_likers(self, info: dict) -> list[str]:
        """這首歌被誰按讚（供頭像 overlay / 診斷用）。歌不存在回 []。"""
        s = self._data.get("songs", {}).get(self._key(info), {})
        return list((s.get("likes") or {}).keys())

    def is_requester(self, info: dict, username: str) -> bool:
        """username 是否真人點播過這首歌（掛名推薦的資格檢查）。

        exact key 比對——「Marvin推薦（為X）」偽 requester 不等於 X 本人點過。
        """
        if not username:
            return False
        s = self._data.get("songs", {}).get(self._key(info))
        return bool(s and s.get("requesters", {}).get(username))

    def undo_play(self, info: dict, requested_by: str | None = None) -> bool:
        """抹除一次 record_play（誤點救回）：反向抵銷最近一次播放紀錄。

        2026-07-01：使用者要求「偶爾點錯的歌播出來時，能當下把它從記憶抹去」，
        避免污染口味指紋（_human_plays 從 requesters 算）與 autopilot 種子。

        - pop 掉最後一筆 plays（誤點那次），total_plays -1
        - requesters[點播者] -1，歸零則移除該 key（requested_by 未給則取該筆的 by）
        - 已無真人播放且無 reactions → 整首移除（不再當推薦種子）
        找不到這首 → 回 False（no-op）。
        """
        key = self._key(info)
        songs = self._data.get("songs", {})
        s = songs.get(key)
        if not s:
            return False
        who = requested_by
        plays = s.get("plays", [])
        if plays:
            who = who or plays.pop().get("by")
        s["total_plays"] = max(0, s.get("total_plays", 0) - 1)
        reqs = s.setdefault("requesters", {})
        if who and who in reqs:
            reqs[who] -= 1
            if reqs[who] <= 0:
                del reqs[who]
        # 真人播放計數：排除 'Marvin推薦（為X）' 等自薦（同 taste_fingerprint._is_human）
        human_left = sum(c for r, c in reqs.items()
                         if r and "Marvin" not in r and "推薦" not in r)
        if human_left <= 0 and not s.get("reactions"):
            del songs[key]
        self._save()
        return True

    def record_reactions(self, info: dict, reactions: dict):
        """reactions: {username: {feelings: [], quotes: [], lyric_match: str}}"""
        key = self._key(info)
        s = self._data.get("songs", {}).get(key)
        if not s:
            return
        for username, r in reactions.items():
            ex = s.setdefault("reactions", {}).setdefault(username, {})
            merged = list(dict.fromkeys(ex.get("feelings", []) + r.get("feelings", [])))
            ex["feelings"] = merged[:10]
            quotes = ex.get("quotes", [])
            for q in r.get("quotes", []):
                if q and q not in quotes:
                    quotes.append(q)
            ex["quotes"] = quotes[-5:]
            if r.get("lyric_match"):
                ex["lyric_match"] = r["lyric_match"]
            if username not in s.setdefault("connections", []):
                s["connections"].append(username)
        self._save()

    def add_recommendation_feedback(self, username: str, title: str, result: str):
        """result: 'liked' | 'skipped' | 'played_again'"""
        u = self._data.setdefault("recommendations", {}).setdefault(
            username, {"feedback": []}
        )
        u["feedback"].append({"title": title, "result": result, "ts": time.time()})
        u["feedback"] = u["feedback"][-20:]
        self._save()

    def add_recent_recommendation(self, title: str):
        """記錄一次自動推薦（group-level ring，活過重啟），供 novelty 排除。"""
        if not title:
            return
        ring = self._data.setdefault("recent_recommendations", [])
        ring.append({"title": title, "ts": time.time()})
        self._data["recent_recommendations"] = ring[-40:]
        self._save()

    # 自動推薦 novelty 視窗：超過此秒數的舊推薦不再 exclude，避免 ring 變永久
    # 黑名單把 recommender 餓死（熱門歌一進 ring 就被永久 ban → enqueued=0）。
    RECOMMENDATION_NOVELTY_TTL_S = 24 * 3600

    def get_recent_recommendation_titles(self, ttl_s: float | None = None) -> list[str]:
        """最近 ttl_s 內自動推薦過的歌名（供 exclude，避免短期重複推薦）。

        用每筆 entry 的 ts 做時間衰減：舊推薦過 TTL 後重新可選，ring 不再是
        永久黑名單。ttl_s=None → 用預設 RECOMMENDATION_NOVELTY_TTL_S。
        """
        if ttl_s is None:
            ttl_s = self.RECOMMENDATION_NOVELTY_TTL_S
        now = time.time()
        return [
            e.get("title", "")
            for e in self._data.get("recent_recommendations", [])
            if e.get("title") and now - e.get("ts", 0) < ttl_s
        ]

    def get_skipped_titles(self, usernames: list[str]) -> list[str]:
        """每位使用者「**最新**一筆 feedback 是 skipped」的歌名（供 exclude / 降權）。

        latest-wins：較新的 liked / played_again（如 skip 後手動點回）覆蓋舊的 skipped →
        不再永久排除。feedback 是 append-only，list 末尾 = 最新。
        """
        out: list[str] = []
        recs = self._data.get("recommendations", {})
        for u in usernames:
            latest: dict[str, str] = {}   # title → 最新 result
            for f in recs.get(u, {}).get("feedback", []):
                t = f.get("title")
                if t and f.get("result"):
                    latest[t] = f["result"]
            out.extend(t for t, r in latest.items() if r == "skipped")
        return out

    # ── 自動點播排除：穩定 videoId（取代脆弱的歌名比對） ──────────────────────

    def record_skipped_video_id(self, url: str) -> None:
        """把 skip 掉的歌的 videoId 記入**永久**排除集（survives restart）。

        2026-06-14：使用者要求「按過下一首的歌不要再自動點」。用 videoId 而非
        歌名（穩定、不被 latest-wins 覆蓋）。非 YouTube / 抽不到 id → no-op。
        """
        vid = extract_video_id(url)
        if not vid:
            return
        lst = self._data.setdefault("skipped_video_ids", [])
        if vid not in lst:
            lst.append(vid)
            self._save()

    def get_skipped_video_ids(self) -> set[str]:
        """永久 skip 排除集（自動點播一律排除）。"""
        return set(self._data.get("skipped_video_ids", []))

    def record_artist_skip(self, artist: str, url: str) -> None:
        """記錄某藝人的歌被 skip（per-artist distinct video-id set）。

        供 Step 3 explore 藝人級 retreat：累計多首被 skip → 該方向探索停手。
        非 YouTube / 無藝人 → no-op。
        """
        vid = extract_video_id(url)
        if not artist or not vid:
            return
        m = self._data.setdefault("artist_skips", {})
        lst = m.setdefault(artist, [])
        if vid not in lst:
            lst.append(vid)
            self._save()

    def get_explore_avoid_artists(self, min_distinct: int = 2) -> list[str]:
        """累計 ≥ min_distinct 首不同歌被 skip 的藝人 → explore 應避開的方向。

        caller（voice_controller）會再扣掉指紋核心藝人（核心被 skip 是單曲層級，
        不代表整個藝人方向爛）。
        """
        m = self._data.get("artist_skips", {})
        return [a for a, vids in m.items() if len(vids) >= min_distinct]

    def get_reacted_seed_ids(self, usernames: list[str]) -> list[str]:
        """有明顯反應（feelings 非空）且**沒被 skip** 的歌的 videoId → 升級成 T2 seed。

        Step 3 promotion：把「有中的驚喜」（含 Marvin 發現後大家有感的歌）拉進未來
        探索種子。只算在場成員的反應；被 skip 的歌（負訊號）排除。
        """
        members = set(usernames)
        skipped = self.get_skipped_video_ids()
        out: list[str] = []
        seen: set[str] = set()
        for url, s in (self._data.get("songs") or {}).items():
            rx = s.get("reactions") or {}
            if not any(rx.get(u, {}).get("feelings") for u in members):
                continue
            vid = extract_video_id(s.get("webpage_url") or url)
            if vid and vid not in skipped and vid not in seen:
                seen.add(vid)
                out.append(vid)
        return out

    def get_recently_played_video_ids(self, ttl_s: float) -> set[str]:
        """ttl_s 內播放過的歌的 videoId（拉長視窗排除，非永久 → 防候選枯竭）。

        衍生自 songs 的 plays 時戳（record_play 已持久化全部播放）→ 自動 survives
        restart，不需另存。超過視窗的老歌重新可選（T3 回收層另會放寬）。
        """
        now = time.time()
        out: set[str] = set()
        for url, s in (self._data.get("songs") or {}).items():
            plays = s.get("plays") or []
            latest = max((p.get("ts", 0) for p in plays), default=0)
            if latest and now - latest < ttl_s:
                vid = extract_video_id(s.get("webpage_url") or url)
                if vid:
                    out.add(vid)
        return out

    def get_recently_played_titles(self, ttl_s: float) -> list[str]:
        """ttl_s 內播放過的歌名（給 T3 relaxed_pool exclude，與 get_recently_played_video_ids
        對齊：否則池子按歌名挑出剛播的歌、enqueue 迴圈又按 video-id 擋掉 → 候選 0 → 停播）。"""
        now = time.time()
        out: list[str] = []
        for url, s in (self._data.get("songs") or {}).items():
            plays = s.get("plays") or []
            latest = max((p.get("ts", 0) for p in plays), default=0)
            if latest and now - latest < ttl_s:
                t = s.get("title")
                if t:
                    out.append(t)
        return out

    def get_liked_video_ids(self, usernames: list[str]) -> list[str]:
        """在場成員 liked 過的歌的 YouTube videoId（T2 radio seed 用，正向訊號）。

        用 normalize_title 把 liked feedback 標題 match 到 songs，取 songs dict key
        （watch URL）的 videoId。去重、保序。不用 skipped 當 seed（避免往被嫌方向擴）。
        """
        import re
        from music_recommender import normalize_title
        liked_norms: set[str] = set()
        recs = self._data.get("recommendations", {})
        for u in usernames:
            for f in recs.get(u, {}).get("feedback", []):
                if f.get("result") == "liked" and f.get("title"):
                    liked_norms.add(normalize_title(f["title"]))
        if not liked_norms:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for url, s in (self._data.get("songs") or {}).items():
            if normalize_title(s.get("title", "")) in liked_norms:
                m = re.search(r"(?:v=|youtu\.be/|/watch\?v=)([A-Za-z0-9_-]{11})", url or "")
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    out.append(m.group(1))
        return out

    def get_played_seed_ids(self, usernames: list[str], limit: int = 20) -> list[str]:
        """在場成員**真人點過**的歌的 videoId（T2 radio 多 seed 來源，比 liked 更廣）。

        關鍵守則：排除「Marvin推薦（為X）」等自薦 requester（避免回音室——拿自己推的
        歌當 seed 會讓推薦越收越窄）。只算在場成員的真人點播次數，按次數加權取 top-N。
        videoId 從 songs dict key（watch URL）抽，與 get_liked_video_ids 一致。
        """
        import re
        members = set(usernames)
        weighted: list[tuple[int, str]] = []
        for url, s in (self._data.get("songs") or {}).items():
            human = sum(
                cnt for r, cnt in (s.get("requesters") or {}).items()
                if r in members and "Marvin" not in r and "推薦" not in r
            )
            if human <= 0:
                continue
            m = re.search(r"(?:v=|youtu\.be/|/watch\?v=)([A-Za-z0-9_-]{11})", url or "")
            if m:
                weighted.append((human, m.group(1)))
        weighted.sort(key=lambda x: -x[0])
        out: list[str] = []
        seen: set[str] = set()
        for _, vid in weighted:
            if vid not in seen:
                seen.add(vid)
                out.append(vid)
            if len(out) >= limit:
                break
        return out

    def get_recent_feedback(self, username: str, since_ts: float) -> list[dict]:
        """Read-only: return recommendation feedback entries for user, ts >= since_ts.

        Used by T2 threshold writer to count consecutive same-direction feedbacks
        before promoting to suki likes/dislikes. Per `feedback_slow_learning_via_recommendations.md` Section 3a rules.
        """
        bucket = (
            self._data.get("recommendations", {})
                      .get(username, {})
                      .get("feedback", [])
        )
        return [e for e in bucket if e.get("ts", 0) >= since_ts]

    # ── Read / Context ─────────────────────────────────────────────────────

    def get_user_music_context(self, username: str, exclude: list[str] | None = None) -> str:
        """組合可直接注入 LLM prompt 的使用者音樂背景字串。
        exclude: 標題清單，這些歌不會出現在 context 裡（避免 LLM 偏向推薦同一首）。
        """
        songs = self._data.get("songs", {})
        exclude_set = {t.lower() for t in (exclude or [])}
        lines = []

        # 1. 點播記錄（按次數排序，排除近期已播/已推薦）
        user_songs = sorted(
            [(s, s["requesters"].get(username, 0))
             for s in songs.values()
             if username in s.get("requesters", {})
             and s.get("title", "").lower() not in exclude_set],
            key=lambda x: x[1], reverse=True
        )
        if user_songs:
            lines.append(f"【{username} 的點播記憶】")
            for s, cnt in user_songs[:6]:
                user_plays = [p for p in s.get("plays", []) if p["by"] == username]
                slots = [p["time_slot"] for p in user_plays]
                slot = max(set(slots), key=slots.count) if slots else "不詳"
                line = f"  •《{s['title']}》共 {cnt} 次，常在{slot}聽"
                r = s.get("reactions", {}).get(username, {})
                if r.get("feelings"):
                    line += f"，感受：{' / '.join(r['feelings'][:3])}"
                lines.append(line)
                if r.get("lyric_match"):
                    lines.append(f"    ↳ {r['lyric_match'][:100]}")

        # 2. 跨人共鳴
        shared = [
            (s["title"], [u for u in s.get("connections", []) if u != username])
            for s in songs.values()
            if username in s.get("connections", []) and len(s.get("connections", [])) > 1
        ]
        if shared:
            lines.append("【與他人共鳴的歌】")
            for title, others in shared[:3]:
                lines.append(f"  •《{title}》：{username} 與 {'、'.join(others[:2])} 都有感觸")

        # 3. 推薦回饋摘要
        feedback = self._data.get("recommendations", {}).get(username, {}).get("feedback", [])
        if feedback:
            liked   = [f["title"] for f in feedback if f["result"] == "liked"][-3:]
            skipped = [f["title"] for f in feedback if f["result"] == "skipped"][-3:]
            if liked:
                lines.append(f"【之前推薦中喜歡的】：{', '.join(liked)}")
            if skipped:
                lines.append(f"【不感興趣的】：{', '.join(skipped)}")

        return "\n".join(lines)

    def all_songs(self) -> dict:
        """完整 songs 字典快照（供 music_recommender 候選池 builder）。"""
        return self._data.get("songs", {})

    def get_top_songs_for_user(self, username: str, limit: int = 10) -> list:
        songs = self._data.get("songs", {})
        ranked = sorted(
            [s for s in songs.values() if username in s.get("requesters", {})],
            key=lambda s: s["requesters"][username], reverse=True
        )
        return ranked[:limit]

    # ── STT Correction ────────────────────────────────────────────────────

    def record_stt_correction(self, username: str, wrong: str, correct: str):
        """記錄一筆語音辨識錯誤與使用者手動修正的對應。"""
        bucket = self._data.setdefault("stt_corrections", {}).setdefault(username, [])
        for entry in bucket:
            if entry["wrong"] == wrong:
                entry["correct"] = correct
                entry["count"] = entry.get("count", 0) + 1
                self._save()
                return
        bucket.append({"wrong": wrong, "correct": correct, "count": 1})
        # 最多保留 100 筆
        self._data["stt_corrections"][username] = bucket[-100:]
        self._save()

    def apply_stt_correction(self, username: str, query: str) -> tuple[str, str | None]:
        """
        嘗試修正語音辨識錯誤。
        回傳 (corrected_query, original_wrong) — 若無修正則 original_wrong 為 None。
        """
        bucket = self._data.get("stt_corrections", {}).get(username, [])
        if not bucket:
            return query, None

        # 1. 完全比對
        for entry in bucket:
            if entry["wrong"] == query:
                logger.info(f"🔧 [STT] 完全比對修正: '{query}' → '{entry['correct']}'")
                return entry["correct"], query

        # 2. 模糊比對（rapidfuzz，threshold 78）— rapidfuzz 在 module top 已 import
        if _rapidfuzz_fuzz is not None:
            best_score, best_entry = 0, None
            for entry in bucket:
                score = _rapidfuzz_fuzz.ratio(entry["wrong"], query)
                if score > best_score:
                    best_score, best_entry = score, entry
            if best_entry and best_score >= 78:
                logger.info(f"🔧 [STT] 模糊比對修正 ({best_score:.0f}%): '{query}' → '{best_entry['correct']}'")
                return best_entry["correct"], query

        return query, None


# ── 推薦掛名規則（2026-07-02 使用者訂） ─────────────────────────────────────

GROUP_ATTRIBUTION = "Marvin推薦（點給大家）"


def recommend_attribution(mm, info: dict, spotlight: str) -> str:
    """自動推薦的 requested_by 掛名：「為X」⟹ X 真的點過這首，否則點給大家。

    背景：discovery 新歌 / themed 主題歌單掛「為X」但 X 根本沒點過 →
    使用者混淆。fail-safe 一律回團體掛名（掛錯名比不掛名傷）。
    兩種字串都含「Marvin」「推薦」→ taste/_is_human 排除邏輯不受影響。
    """
    try:
        if mm is not None and spotlight and mm.is_requester(info, spotlight):
            return f"Marvin推薦（為{spotlight}）"
    except Exception:
        pass
    return GROUP_ATTRIBUTION
