import json
import logging
import sqlite3
from datetime import datetime

logger = logging.getLogger("SukiBudget")

_DB_PATH = "marvin.db"


class SukiBudget:
    """
    Suki 預算與熔斷器。
    後端：SQLite (WAL mode)，與 MemoryManager 共用同一個 DB 檔案。
    """

    def __init__(self, db_path: str = _DB_PATH, max_tokens: int = 500_000):
        self._db_path = db_path
        self.max_tokens = max_tokens
        self._conn = self._open_db()
        self._check_reset()

    # ── DB init ──────────────────────────────────────────────────────────────

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS budget "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        return conn

    # ── Key-value helpers ────────────────────────────────────────────────────

    def _get(self, key: str, default=None):
        row = self._conn.execute("SELECT value FROM budget WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key: str, value) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO budget (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
        self._conn.commit()

    # ── Daily reset ──────────────────────────────────────────────────────────

    def _check_reset(self):
        today = datetime.now().strftime("%Y-%m-%d")
        stored = self._get("date", "")
        if stored != today:
            if stored:
                logger.info(f"📅 [Budget] 日期變更 ({stored} → {today})，預算歸零。")
            self._set("date", today)
            self._set("total_tokens", 0)
            self._set("has_warned_80", False)
            self._set("has_warned_95", False)

    # ── Public API (unchanged from original) ─────────────────────────────────

    def add_tokens(self, count: int) -> dict:
        self._check_reset()
        total = self._get("total_tokens", 0) + count
        self._set("total_tokens", total)
        pct = (total / self.max_tokens) * 100
        status = {
            "trigger_80": False,
            "trigger_95": False,
            "is_exhausted": total >= self.max_tokens,
            "current_percentage": pct,
        }
        if pct >= 80 and not self._get("has_warned_80", False):
            status["trigger_80"] = True
            self._set("has_warned_80", True)
            logger.warning(f"⚠️ [Budget] 跨越 80% 水位線 ({total} tokens)")
        if pct >= 95 and not self._get("has_warned_95", False):
            status["trigger_95"] = True
            self._set("has_warned_95", True)
            logger.error(f"🚨 [Budget] 跨越 95% 水位線 ({total} tokens)")
        return status

    def is_circuit_open(self) -> bool:
        self._check_reset()
        return self._get("total_tokens", 0) >= self.max_tokens

    def get_info(self) -> dict:
        total = self._get("total_tokens", 0)
        return {
            "used": total,
            "max": self.max_tokens,
            "percentage": (total / self.max_tokens) * 100,
        }

    @property
    def tokens(self) -> int:
        return self._get("total_tokens", 0)

    @property
    def total_limit(self) -> int:
        return self.max_tokens
