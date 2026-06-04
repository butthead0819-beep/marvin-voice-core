"""
🎵 MusicMemory — 個人音樂記憶系統
追蹤點播記錄、情感反應、歌詞共鳴與推薦回饋。
"""
import json
import os
import time
import datetime
import logging

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
        """這些使用者標記為 skipped 的推薦歌名（供 exclude / 降權）。"""
        out: list[str] = []
        recs = self._data.get("recommendations", {})
        for u in usernames:
            for f in recs.get(u, {}).get("feedback", []):
                if f.get("result") == "skipped" and f.get("title"):
                    out.append(f["title"])
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
