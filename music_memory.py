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


class MusicMemory:

    def __init__(self, path: str = "music_memory.json"):
        self.path = path
        self._data = self._load()

    # ── Persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"songs": {}, "recommendations": {}}

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
        return info.get("url") or f"{info.get('title', '')}|{info.get('uploader', '')}"

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

    def get_recent_recommendation_titles(self) -> list[str]:
        """最近自動推薦過的歌名（供 exclude，避免重複推薦）。"""
        return [
            e.get("title", "")
            for e in self._data.get("recent_recommendations", [])
            if e.get("title")
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
