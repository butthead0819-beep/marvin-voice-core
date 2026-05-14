import copy
import json
import logging
import os
import sqlite3
import time
from datetime import datetime

logger = logging.getLogger("SukiMemory")

_DB_PATH = "marvin.db"
_JSON_COMPAT_PATH = "suki_memory.json"

_PLAYER_DEFAULTS: dict = {
    "personal_info": {
        "food": None, "clothing": None,
        "housing": None, "transport": None, "minecraft_id": None,
    },
    "likes": [],
    "dislikes": [],
    "taboos": [],
    "stats": {
        "interaction_count": 0,
        "pos_feedback": 0,
        "neg_feedback": 0,
        "vul_feedback": 0,
    },
    "news_queue": [],
    "song_history": [],
    "suki_impression": "",
    "bias_score": 0,
    "last_interacted_time": 0.0,
    "emotional_highlights": [],
    "behavioral_patterns": {},
    "relationship_stage": "陌生人",
    "relationship_note": "",
    "speech_dna": {},
}


def _new_player() -> dict:
    return copy.deepcopy(_PLAYER_DEFAULTS)


def _repair_player(p: dict) -> dict:
    """Fill in any fields missing from older records (non-destructive)."""
    if not isinstance(p, dict):
        return _new_player()
    for k, v in _PLAYER_DEFAULTS.items():
        if k not in p:
            p[k] = copy.deepcopy(v)
    pi = p.setdefault("personal_info", {})
    for k in ("food", "clothing", "housing", "transport", "minecraft_id"):
        pi.setdefault(k, None)
    stats = p.setdefault("stats", {})
    for k in ("interaction_count", "pos_feedback", "neg_feedback", "vul_feedback"):
        stats.setdefault(k, 0)
    return p


class MemoryManager:
    """
    Marvin 長期記憶倉庫。
    後端：SQLite (WAL mode) + JSON export for script compatibility.
    """

    def __init__(self, db_path: str = _DB_PATH, json_compat_path: str = _JSON_COMPAT_PATH):
        self._db_path = db_path
        self._json_compat_path = json_compat_path
        self._conn = self._open_db()
        self._cache: dict[str, dict] = {}
        self._load_all()

    # ── DB init ──────────────────────────────────────────────────────────────

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS players "
            "(username TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}')"
        )
        # 氣氛校正資料表（Companion 回饋的 too_loud / too_sharp / too_jolly）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS atmosphere_corrections ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "snapshot_ts REAL NOT NULL, "
            "label TEXT NOT NULL, "
            "speaker TEXT, "
            "created_ts REAL NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_atmos_label "
            "ON atmosphere_corrections(label)"
        )
        conn.commit()
        return conn

    # ── Load / migrate ───────────────────────────────────────────────────────

    def _load_all(self):
        rows = self._conn.execute("SELECT username, data FROM players").fetchall()
        if not rows:
            self._migrate_from_json()
            rows = self._conn.execute("SELECT username, data FROM players").fetchall()
        for username, data_str in rows:
            try:
                self._cache[username] = _repair_player(json.loads(data_str))
            except Exception as exc:
                logger.warning(f"⚠️ [Memory] 無法載入 {username}: {exc}")

    def _migrate_from_json(self):
        """One-time import from suki_memory.json (runs only when DB is empty)."""
        if not os.path.exists(self._json_compat_path):
            return
        try:
            with open(self._json_compat_path, "r", encoding="utf-8") as f:
                old = json.load(f)
            players = old.get("players", {})
            for username, pdata in players.items():
                repaired = _repair_player(pdata)
                self._conn.execute(
                    "INSERT OR IGNORE INTO players (username, data) VALUES (?, ?)",
                    (username, json.dumps(repaired, ensure_ascii=False)),
                )
            self._conn.commit()
            logger.info(f"✅ [Memory] 已從 JSON 遷移 {len(players)} 名玩家至 SQLite。")
        except Exception as exc:
            logger.error(f"❌ [Memory] JSON 遷移失敗: {exc}")

    # ── Persist ──────────────────────────────────────────────────────────────

    def _save_player(self, username: str):
        if username not in self._cache:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO players (username, data) VALUES (?, ?)",
            (username, json.dumps(self._cache[username], ensure_ascii=False)),
        )
        self._conn.commit()
        self._export_json()

    def _export_json(self):
        """Write suki_memory.json so external scripts can still read it."""
        try:
            tmp = self._json_compat_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"players": self._cache}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._json_compat_path)
        except Exception as exc:
            logger.warning(f"⚠️ [Memory] JSON 導出失敗 (不影響主功能): {exc}")

    def flush(self):
        """No-op: SQLite writes are immediate. Kept for API compatibility."""

    # ── Player access ────────────────────────────────────────────────────────

    def get_player_memory(self, username: str) -> dict:
        if username not in self._cache:
            p = _new_player()
            p["last_interacted_time"] = time.time()
            self._cache[username] = p
            self._save_player(username)
        self._cache[username]["last_interacted_time"] = time.time()
        return self._cache[username]

    def increment_stat(self, username: str, field: str, delta: float = 1.0):
        stats = self.get_player_memory(username)["stats"]
        stats[field] = float(stats.get(field, 0)) + float(delta)
        self._save_player(username)

    def enqueue_news(self, username: str, news_text: str):
        queue = self.get_player_memory(username)["news_queue"]
        queue.append({"text": news_text, "timestamp": time.time()})
        if len(queue) > 3:
            queue.pop(0)
        self._save_player(username)

    def pop_news(self, username: str) -> str | None:
        if username not in self._cache:
            return None
        queue = self._cache[username]["news_queue"]
        if not queue:
            return None
        news = queue.pop(0)
        self._save_player(username)
        return news["text"]

    def get_player_impression(self, username: str) -> str:
        return self.get_player_memory(username).get("suki_impression", "")

    def set_player_impression(self, username: str, impression: str):
        self.get_player_memory(username)["suki_impression"] = impression
        self._save_player(username)

    def set_minecraft_id(self, username: str, mc_id: str):
        self.get_player_memory(username)["personal_info"]["minecraft_id"] = mc_id
        self._save_player(username)
        logger.info(f"🧱 [Memory] {username} 已綁定 Minecraft ID: {mc_id}")

    def update_player_memory(self, username: str, new_info: dict):
        player = self.get_player_memory(username)
        if "personal_info" in new_info:
            for k, v in new_info["personal_info"].items():
                if v is not None:
                    player["personal_info"][k] = v
        for key in ("likes", "dislikes", "taboos"):
            if key in new_info and isinstance(new_info[key], list):
                current = set(player.get(key, []))
                current.update(item for item in new_info[key] if item)
                player[key] = list(current)
        self._save_player(username)
        logger.info(f"🧠 [Memory] 已更新 {username} 的記憶庫。")

    def mark_taboo(self, username: str, topic: str):
        player = self.get_player_memory(username)
        if topic and topic not in player["taboos"]:
            player["taboos"].append(topic)
            self._save_player(username)
            logger.warning(f"🚫 [Memory] {username} 將話題『{topic}』列為禁忌。")

    def get_missing_info_categories(self, username: str) -> list:
        mem = self.get_player_memory(username)
        taboos = mem.get("taboos", [])
        return [k for k, v in mem.get("personal_info", {}).items() if v is None and k not in taboos]

    def get_known_info(self, username: str) -> list:
        mem = self.get_player_memory(username)
        taboos = mem.get("taboos", [])
        return [(k, v) for k, v in mem.get("personal_info", {}).items() if v is not None and k not in taboos]

    def adjust_bias(self, username: str, delta: float):
        p = self.get_player_memory(username)
        before = float(p.get("bias_score", 0))
        p["bias_score"] = max(-10.0, min(10.0, before + float(delta)))
        self._save_player(username)
        logger.debug(f"🎭 [Bias] {username}: {before:.1f} → {p['bias_score']:.1f} ({delta:+.1f})")

    def find_shared_interests(self, active_users: list) -> str | None:
        if len(active_users) < 2:
            return None
        data = {u: self.get_player_memory(u) for u in active_users}
        points = []
        names = list(data.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                u1, u2 = names[i], names[j]
                common = set(data[u1]["likes"]) & set(data[u2]["likes"])
                if common:
                    points.append(f"玩家 {u1} 和 {u2} 有共同愛好: {', '.join(common)}")
                p1, p2 = data[u1]["personal_info"], data[u2]["personal_info"]
                for k, v1 in p1.items():
                    if v1 and v1 == p2.get(k) and v1 not in ("未提及", "未知", "無"):
                        points.append(f"玩家 {u1} 和 {u2} 的『{k}』一樣: {v1}")
        return "\n".join(points) if points else None

    def add_song_history(self, username: str, song_title: str):
        p = self.get_player_memory(username)
        history = p.get("song_history", [])
        if song_title in history:
            history.remove(song_title)
        history.append(song_title)
        p["song_history"] = history[-20:]
        self._save_player(username)

    def get_song_history(self, username: str) -> list:
        if username not in self._cache:
            return []
        return self._cache[username].get("song_history", [])

    def get_proactive_topics(self) -> list:
        return []

    # ── Layer 2-4: emotional / behavioural / relationship ────────────────────

    def add_emotional_highlight(self, username: str, moment: str, valence: str = "warm"):
        if not username or not moment:
            return
        p = self.get_player_memory(username)
        highlights = p.get("emotional_highlights", [])
        highlights.append({"moment": moment, "valence": valence, "timestamp": time.time()})
        p["emotional_highlights"] = highlights[-5:]
        self._save_player(username)
        logger.debug(f"💜 [WarmCircuit] {username}: {moment[:20]}... ({valence})")

    def update_relationship(self, username: str, stage: str, note: str = ""):
        p = self.get_player_memory(username)
        old = p.get("relationship_stage", "陌生人")
        p["relationship_stage"] = stage
        if note:
            p["relationship_note"] = note
        self._save_player(username)
        if old != stage:
            logger.info(f"💜 [WarmCircuit] {username} 關係進化: {old} → {stage}")

    def update_behavioral_pattern(self, username: str, key: str, value: str):
        p = self.get_player_memory(username)
        p.setdefault("behavioral_patterns", {})[key] = value
        self._save_player(username)
        logger.debug(f"🔄 [WarmCircuit] {username} 行為記憶: {key} = {value}")

    def get_rich_context(self, username: str) -> str:
        mem = self.get_player_memory(username)
        lines = []
        stage = mem.get("relationship_stage", "陌生人")
        count = mem.get("stats", {}).get("interaction_count", 0)
        lines.append(f"[🤝 關係溫度]：{stage}（已互動 {count} 次）")
        note = mem.get("relationship_note", "")
        if note:
            lines.append(f"[📝 關係備忘]：{note}")
        highlights = mem.get("emotional_highlights", [])
        if highlights:
            latest = highlights[-1]
            valence_map = {
                "warm": "感到一絲異樣的溫暖",
                "surprised": "出乎意料地",
                "moved": "心底某個角落有點波動",
                "annoyed": "感到加倍的沮喪",
            }
            desc = valence_map.get(latest.get("valence", "warm"), "有些情緒")
            lines.append(f"[💜 我記得的瞬間]：{latest['moment']}（當時我{desc}）")
        queue = mem.get("news_queue", [])
        if queue:
            latest_news = queue[-1]
            if time.time() - latest_news.get("timestamp", 0) < 86400:
                lines.append(f"[🗞️ 最新話題]：{latest_news.get('text', '')}")
        return "\n".join(lines) if lines else ""

    # ── Speech DNA ───────────────────────────────────────────────────────────

    def get_speech_dna(self, username: str) -> dict:
        return self.get_player_memory(username).get("speech_dna") or {}

    def update_speech_dna(self, username: str, dna: dict) -> None:
        p = self.get_player_memory(username)
        dna["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        p["speech_dna"] = dna
        self._save_player(username)
        logger.info(f"🎭 [SpeechDNA] {username} 說話 DNA 已更新")

    # ── Atmosphere corrections（Companion 回饋） ─────────────────────────────

    def record_atmosphere_correction(
        self,
        snapshot_ts: float,
        label: str,
        speaker: str | None = None,
    ) -> None:
        """記錄一筆氣氛回饋。label ∈ {"too_loud", "too_sharp", "too_jolly"}。"""
        self._conn.execute(
            "INSERT INTO atmosphere_corrections "
            "(snapshot_ts, label, speaker, created_ts) VALUES (?, ?, ?, ?)",
            (float(snapshot_ts), str(label), speaker, time.time()),
        )
        self._conn.commit()
        logger.debug(
            f"🌡  [Memory] 已寫入氣氛校正 label={label} speaker={speaker}"
        )

    def get_atmosphere_calibration(self) -> dict:
        """回傳累積的氣氛校正資料，供 AtmosphereTracker._load_calibration() 使用。

        回傳結構：
          {
            "label_counts": {"too_loud": N, "too_sharp": N, "too_jolly": N},
            "recent_corrections": [{snapshot_ts, label, speaker, created_ts}, ...]
          }
        recent_corrections 以 created_ts DESC 排序，最多 50 筆。
        """
        counts_rows = self._conn.execute(
            "SELECT label, COUNT(*) FROM atmosphere_corrections GROUP BY label"
        ).fetchall()
        label_counts = {label: int(n) for label, n in counts_rows}

        recent_rows = self._conn.execute(
            "SELECT snapshot_ts, label, speaker, created_ts "
            "FROM atmosphere_corrections "
            "ORDER BY created_ts DESC, id DESC LIMIT 50"
        ).fetchall()
        recent_corrections = [
            {
                "snapshot_ts": snapshot_ts,
                "label": label,
                "speaker": speaker,
                "created_ts": created_ts,
            }
            for (snapshot_ts, label, speaker, created_ts) in recent_rows
        ]

        return {
            "label_counts": label_counts,
            "recent_corrections": recent_corrections,
        }
