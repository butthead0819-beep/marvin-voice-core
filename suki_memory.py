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

# ── 主動 callback 記憶（proactive group-memory callback, eng-review locked）──────
# 專用 callback_queue，與 news_queue 分開：不撞 news 的 cap 淘汰、不被 get_rich_context
# 注入 reactive prompt（fail-private 真成立）、自己的 TTL/render。
_CALLBACK_CAP = 10                  # 上限（比 news 大，這是主打功能）
_CALLBACK_TTL_SECONDS = 7 * 86400   # 未投遞 callback 7 天過期，避免幾週後詭異冒出

# ── taste 分數分級（per feedback_dual_path_taste_writes 的正解）─────────────────
# likes/dislikes 是 taste 分數的投影；分數 ≥/≤ 閾值才算 confirmed，中間＝「曾提及」。
# taboos 維持獨立（敏感標記，不被分數投影）。
LIKE_THRESHOLD = 3.0
DISLIKE_THRESHOLD = -3.0
_SCORE_MIN, _SCORE_MAX = -10.0, 10.0
_MIGRATE_SCORE = 3.0     # 舊 likes/dislikes 遷入 taste 的起始分（剛過閾值＝confirmed 但可被約 3 個反向訊號重新調整）


def _build_taste_from_legacy(likes: list, dislikes: list) -> dict:
    """舊式二元 likes/dislikes → taste 分數 dict（confirmed 起始分）。taboos 不納入。"""
    now = time.time()
    taste: dict = {}
    for item in likes or []:
        if item:
            taste[item] = {"score": _MIGRATE_SCORE, "mentions": 1, "first_seen": now, "last_update": now}
    for item in dislikes or []:
        if item:
            taste[item] = {"score": -_MIGRATE_SCORE, "mentions": 1, "first_seen": now, "last_update": now}
    return taste


def _project_taste(player: dict) -> None:
    """從 taste 分數重算 player['likes']/['dislikes']（taboos 不動）。likes 按分數高→低。"""
    taste = player.get("taste", {})
    likes = sorted((i for i, d in taste.items() if d.get("score", 0) >= LIKE_THRESHOLD),
                   key=lambda i: -taste[i].get("score", 0))
    dislikes = sorted((i for i, d in taste.items() if d.get("score", 0) <= DISLIKE_THRESHOLD),
                      key=lambda i: taste[i].get("score", 0))
    player["likes"] = likes
    player["dislikes"] = dislikes


_PLAYER_DEFAULTS: dict = {
    "personal_info": {
        "food": None, "clothing": None,
        "housing": None, "transport": None, "minecraft_id": None,
    },
    "likes": [],
    "dislikes": [],
    "taboos": [],
    "taste": {},
    "stats": {
        "interaction_count": 0,
        "pos_feedback": 0,
        "neg_feedback": 0,
        "vul_feedback": 0,
    },
    "news_queue": [],
    "callback_queue": [],
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
    # taste 遷移：舊資料（無 taste 欄位）→ 從 likes/dislikes 建分數。放在 generic fill 前，
    # 否則下方會先補 taste={} 使遷移永不觸發。idempotent：taste 欄位一旦存在（即使空）就不重建。
    if "taste" not in p:
        p["taste"] = _build_taste_from_legacy(p.get("likes", []), p.get("dislikes", []))
        _project_taste(p)
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

    Guild-scoped：每個 guild 一個 instance（用 ``for_guild`` registry 取得）。
    ``players`` 表以 (guild_id, username) 為複合 PK；不傳 guild_id 時預設 home guild
    （env ``GUILD_ID``，無則 0），讓既有單 guild caller 與 offline script 維持原行為。
    JSON compat 只在 home guild 匯出，避免 guest guild 把 home 的 suki_memory.json 蓋掉。
    """

    # guild_id → instance 快取（同一 db_path 內，一個 guild 一個 manager）
    _registry: dict = {}

    def __init__(
        self,
        guild_id: int | None = None,
        db_path: str = _DB_PATH,
        json_compat_path: str = _JSON_COMPAT_PATH,
    ):
        self._home_guild_id = int(os.environ.get("GUILD_ID") or 0)
        self._guild_id = self._home_guild_id if guild_id is None else int(guild_id)
        self._is_home = self._guild_id == self._home_guild_id
        self._db_path = db_path
        self._json_compat_path = json_compat_path
        self._conn = self._open_db()
        self._cache: dict[str, dict] = {}
        self._load_all()

    @classmethod
    def for_guild(
        cls,
        guild_id: int,
        db_path: str = _DB_PATH,
        json_compat_path: str = _JSON_COMPAT_PATH,
    ) -> "MemoryManager":
        """取得（或建立並快取）某 guild 的 MemoryManager。

        Why: 整個 codebase 透過這個 registry 取得 manager，method 簽名維持 username-only，
        guild scoping 烤進「你拿到哪個 instance」而非每個呼叫都帶 guild_id。
        """
        key = (int(guild_id), db_path)
        inst = cls._registry.get(key)
        if inst is None:
            inst = cls(guild_id=int(guild_id), db_path=db_path, json_compat_path=json_compat_path)
            cls._registry[key] = inst
        return inst

    @classmethod
    def reset_registry(cls) -> None:
        """清空 instance 快取（測試用）。"""
        cls._registry = {}

    # ── DB init ──────────────────────────────────────────────────────────────

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_players_schema(conn)
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

    def _ensure_players_schema(self, conn: sqlite3.Connection) -> None:
        """建立 / 遷移 players 表至 (guild_id, username) 複合 PK。

        舊 schema（username PK，無 guild_id）偵測到時 rebuild，舊資料整批歸 home guild。
        Migration 可重入（idempotent）：rebuild 過程若中途 crash 留下孤兒 players_legacy，
        下次啟動會用 INSERT OR IGNORE 把它補進新表再 DROP，不靠「一次不中斷」、不丟資料。
        """
        new_ddl = (
            "CREATE TABLE players ("
            "guild_id INTEGER NOT NULL, username TEXT NOT NULL, "
            "data TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (guild_id, username))"
        )

        def _has_table(name: str) -> bool:
            return conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None

        home = self._home_guild_id

        # (1) 先復原上次 migration crash 留下的孤兒 players_legacy。
        #     crash 點：RENAME 後（players 不在）或 CREATE 後 INSERT 未完（players 為空新表）。
        #     兩種都用 INSERT OR IGNORE 補進新表再 DROP，已遷的 row 自動跳過。
        if _has_table("players_legacy"):
            if not _has_table("players"):
                conn.execute(new_ddl)
            conn.execute(
                "INSERT OR IGNORE INTO players (guild_id, username, data) "
                "SELECT ?, username, data FROM players_legacy",
                (home,),
            )
            conn.execute("DROP TABLE players_legacy")
            conn.commit()
            logger.info(f"✅ [Memory] 從中斷的 migration 復原 players_legacy（歸 home guild {home}）")
            return

        # (2) 全新 DB
        if not _has_table("players"):
            conn.execute(new_ddl)
            return

        # (3) players 已是新 schema
        cols = [r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()]
        if "guild_id" in cols:
            return

        # (4) 舊 schema → rebuild；若中途 crash，下次啟動由 (1) 的孤兒復原路徑接手。
        conn.execute("ALTER TABLE players RENAME TO players_legacy")
        conn.execute(new_ddl)
        conn.execute(
            "INSERT OR IGNORE INTO players (guild_id, username, data) "
            "SELECT ?, username, data FROM players_legacy",
            (home,),
        )
        conn.execute("DROP TABLE players_legacy")
        conn.commit()
        logger.info(
            f"✅ [Memory] players 表已遷移至 (guild_id, username)；舊資料歸 home guild {home}"
        )

    # ── Load / migrate ───────────────────────────────────────────────────────

    def _load_all(self):
        rows = self._conn.execute(
            "SELECT username, data FROM players WHERE guild_id = ?", (self._guild_id,)
        ).fetchall()
        if not rows and self._is_home:
            self._migrate_from_json()
            rows = self._conn.execute(
                "SELECT username, data FROM players WHERE guild_id = ?", (self._guild_id,)
            ).fetchall()
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
                    "INSERT OR IGNORE INTO players (guild_id, username, data) VALUES (?, ?, ?)",
                    (self._guild_id, username, json.dumps(repaired, ensure_ascii=False)),
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
            "INSERT OR REPLACE INTO players (guild_id, username, data) VALUES (?, ?, ?)",
            (self._guild_id, username, json.dumps(self._cache[username], ensure_ascii=False)),
        )
        self._conn.commit()
        self._export_json()

    def _export_json(self):
        """Write suki_memory.json so external scripts can still read it.

        Preserves top-level meta keys (marvin_performance / proactive_topics 等)
        that daily cron writes — 否則每次 player save 都會把 cron 的成果 nuke 掉。

        只在 home guild 匯出：JSON 的 players 區段是扁平 username map，guest guild
        若也寫會互相蓋掉、也會污染 offline script 讀的 home guild 資料。
        """
        if not self._is_home:
            return
        try:
            # 讀回現有 JSON 以保留 daily cron 寫入的頂層 meta keys
            existing: dict = {}
            if os.path.exists(self._json_compat_path):
                try:
                    with open(self._json_compat_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}

            existing["players"] = self._cache  # players 區段以 cache 為準

            tmp = self._json_compat_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._json_compat_path)
        except Exception as exc:
            logger.warning(f"⚠️ [Memory] JSON 導出失敗 (不影響主功能): {exc}")

    def flush(self):
        """No-op: SQLite writes are immediate. Kept for API compatibility."""

    def replace_player_memory(self, username: str, data: dict):
        """Full-record overwrite — for audit/cleaning pipelines that produce a fresh record."""
        if not isinstance(data, dict):
            raise TypeError(f"replace_player_memory expects dict, got {type(data).__name__}")
        self._cache[username] = _repair_player(dict(data))
        self._save_player(username)

    def list_players(self) -> list[str]:
        """所有已知玩家 username（不會 silently 建立新紀錄）。"""
        return list(self._cache.keys())

    def has_player(self, username: str) -> bool:
        """檢查玩家是否存在；不像 get_player_memory 會 auto-create。"""
        return username in self._cache

    def get_meta(self, key: str, default=None):
        """讀 suki_memory.json 頂層非 players 的 key（daily cron 寫入區）。

        Why: marvin_performance / proactive_topics 由 analyze_daily_log.py 寫到 JSON
        頂層，不在 SQLite players 表內。bot runtime 想讀這些 meta 必須走這條路。
        """
        if not os.path.exists(self._json_compat_path):
            return default
        try:
            with open(self._json_compat_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default
            return data.get(key, default)
        except Exception:
            return default

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

    # ── 主動 callback 記憶（與 news_queue 分開）──────────────────────────────────
    def enqueue_callback(self, username: str, text: str, shareable: bool = False):
        """存一則主動 callback 記憶到 per-player callback_queue。

        shareable=False（fail-private 預設）→ 不會被 peek_shareable_callback 取出，
        也不會進 get_rich_context 的 reactive prompt（callback_queue 不被它讀）。
        """
        if not username or not text:
            return
        queue = self.get_player_memory(username)["callback_queue"]
        queue.append({"text": text, "shareable": bool(shareable), "ts": time.time()})
        if len(queue) > _CALLBACK_CAP:
            del queue[: len(queue) - _CALLBACK_CAP]
        self._save_player(username)

    def peek_shareable_callback(
        self, username: str, ttl_seconds: float = _CALLBACK_TTL_SECONDS
    ) -> dict | None:
        """回傳最舊一則 shareable 且未過期的 callback（不移除——投遞成功才 consume）。

        順手剪掉過期項（TTL）。idempotent 投遞：peek → 投遞 → 成功才 consume_callback，
        失敗則 item 留著下次重投。
        """
        if username not in self._cache:
            return None
        queue = self._cache[username].get("callback_queue", [])
        cutoff = time.time() - ttl_seconds
        fresh = [item for item in queue if item.get("ts", 0) >= cutoff]
        if len(fresh) != len(queue):
            self._cache[username]["callback_queue"] = fresh
            self._save_player(username)
        for item in fresh:
            if item.get("shareable"):
                return item
        return None

    def consume_callback(self, username: str, item: dict):
        """投遞成功後移除該則 callback（以 ts+text 比對）。"""
        if username not in self._cache or not item:
            return
        queue = self._cache[username].get("callback_queue", [])
        self._cache[username]["callback_queue"] = [
            q for q in queue
            if not (q.get("ts") == item.get("ts") and q.get("text") == item.get("text"))
        ]
        self._save_player(username)

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
        # taboos 維持獨立 list；likes/dislikes 導向 taste（confirmed 分）再投影，
        # 確保與 record_taste_signal 共用同一真實來源，_project_taste 不會互相蓋掉。
        if "taboos" in new_info and isinstance(new_info["taboos"], list):
            current = set(player.get("taboos", []))
            current.update(item for item in new_info["taboos"] if item)
            player["taboos"] = list(current)
        now = time.time()
        taste = player.setdefault("taste", {})
        for key, sign in (("likes", _MIGRATE_SCORE), ("dislikes", -_MIGRATE_SCORE)):
            if key in new_info and isinstance(new_info[key], list):
                for item in new_info[key]:
                    if item and item not in taste:
                        taste[item] = {"score": sign, "mentions": 1, "first_seen": now, "last_update": now}
        _project_taste(player)
        self._save_player(username)
        logger.info(f"🧠 [Memory] 已更新 {username} 的記憶庫。")

    def mark_taboo(self, username: str, topic: str):
        player = self.get_player_memory(username)
        if topic and topic not in player["taboos"]:
            player["taboos"].append(topic)
            self._save_player(username)
            logger.warning(f"🚫 [Memory] {username} 將話題『{topic}』列為禁忌。")

    def record_taste_signal(self, username: str, item: str, delta: float, *, reason: str = "") -> None:
        """對某項目 +/- 分（per feedback_dual_path_taste_writes 的統一寫入口）。

        新項目首次有訊號 → 進 taste（曾提及）；累積過 LIKE/DISLIKE_THRESHOLD → 投影到
        likes/dislikes；已歸類的也會因分數升降重新調整。daily review / feedback loop /
        即時語音都該走這裡，不再直接 append likes（兩條 path 加減同一分數 → 不打架）。
        """
        if not item:
            return
        player = self.get_player_memory(username)
        taste = player.setdefault("taste", {})
        now = time.time()
        entry = taste.get(item)
        if entry is None:
            entry = {"score": 0.0, "mentions": 0, "first_seen": now, "last_update": now}
            taste[item] = entry
        entry["score"] = max(_SCORE_MIN, min(_SCORE_MAX, float(entry.get("score", 0)) + float(delta)))
        entry["mentions"] = int(entry.get("mentions", 0)) + 1
        entry["last_update"] = now
        if reason:
            entry["last_reason"] = reason[:100]
        _project_taste(player)
        self._save_player(username)
        logger.debug(f"👅 [Taste] {username} 『{item}』{delta:+.1f} → score={entry['score']:.1f}")

    def remove_taste_item(self, username: str, item: str) -> None:
        """徹底移除一個 taste 項目（含 likes/dislikes/taboos 投影）。用於清掉未確認/否定的資料。"""
        player = self.get_player_memory(username)
        player.get("taste", {}).pop(item, None)
        for key in ("likes", "dislikes", "taboos"):
            lst = player.get(key, [])
            if item in lst:
                lst.remove(item)
        self._save_player(username)
        logger.info(f"🧠 [Taste] {username} 移除項目『{item}』")

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
