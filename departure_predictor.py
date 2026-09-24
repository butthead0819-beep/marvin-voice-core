"""
送客預測模組。

每人各自學「講了下線線索（下線/晚安/拜拜…）後，真的在 5 分鐘內離開」的命中率，
命中率夠準才在人還在時先送客，取代「人走了才送客」的舊行為（見
cogs/voice_controller.py on_voice_state_update Leave 區的改動）。

也記「離場前最後幾句話」給未來的送客文案用，以及回台判斷（斷線重連 vs 真的離開
後再回來）給 Join 區用。

所有時間都用參數傳入的 now/ts，不在內部呼叫 time.time()，方便測試與回放
（scripts/backfill_departure_cues.py 就是靠這點重放歷史資料）。
"""

import json
import os
import re
import logging
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)

_PATH = os.path.join(os.path.dirname(__file__), "departure_cues.json")
CUE_OUTCOME_WINDOW_S = 300      # 線索後 5 分鐘內離開 = 命中
CUE_EPISODE_GAP_S = 120         # 同一人 120s 內的後續線索併入同一段，不另記樣本
MIN_SAMPLES = 8                 # 已結算樣本數門檻
MIN_PRECISION = 0.5             # 命中率門檻
PREFAREWELL_COOLDOWN_S = 600    # 同一人 10 分鐘內只送一次（含喚醒道別）
LAST_WORDS_WINDOW_S = 180       # 離場前 3 分鐘的話
LAST_WORDS_MAX = 5
UTTER_BUFFER_MAX = 20
MAX_CUES = 200
MAX_LAST_WORDS = 100
FLAP_S = 120                    # 離開後 120s 內回來 = 斷線重連，不講話
WELCOME_BACK_S = 3600           # 120s～1 小時內回來 = 簡單招呼

DEPARTURE_CUE_RE = re.compile(
    r'(?:先|要|來|我)下線|下線了|先下了|我先下|我要下了|我下了|要下了|關機'
    r'|先走了|我走了|先閃|我閃了|先離開'
    r'|(?:去|來|要|先|我)睡(?:覺)?|睡覺了|休息了'
    r'|晚安|拜拜|掰掰|再見|明天見|晚點見'
    r'|我要去(?:吃飯|洗澡|當|支援|顧|接|載|上班|開會)'
)


def rejoin_action(gap_s: float | None) -> str:
    """依離開後回來的間隔決定 Join 時要做什麼。"""
    if gap_s is None or gap_s >= WELCOME_BACK_S:
        return "full"
    if gap_s < FLAP_S:
        return "silent"
    return "welcome_back"


class DeparturePredictor:
    def __init__(self, path: str = _PATH):
        self._path = path
        self._data: dict = self._load()
        self._utter: dict[str, deque] = {}
        self._last_farewell: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # I/O                                                                  #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self):
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning(f"[DeparturePredictor] 寫入失敗: {e}")

    def _user(self, speaker: str) -> dict:
        return self._data.setdefault(speaker, {"cues": [], "last_words": []})

    # ------------------------------------------------------------------ #
    # 內部                                                                 #
    # ------------------------------------------------------------------ #

    def _expire(self, speaker: str, ts: float):
        for cue in self._data.get(speaker, {}).get("cues", []):
            if cue["left"] is None and ts - cue["ts"] > CUE_OUTCOME_WINDOW_S:
                cue["left"] = False

    # ------------------------------------------------------------------ #
    # 觀察                                                                 #
    # ------------------------------------------------------------------ #

    def observe(self, speaker: str, text: str, ts: float) -> bool:
        buf = self._utter.setdefault(speaker, deque(maxlen=UTTER_BUFFER_MAX))
        buf.append((ts, text))

        self._expire(speaker, ts)

        if not DEPARTURE_CUE_RE.search(text):
            return False

        user = self._user(speaker)
        cues = user["cues"]
        if not cues or ts - cues[-1]["ts"] >= CUE_EPISODE_GAP_S:
            dt = datetime.fromtimestamp(ts)
            cues.append({"ts": ts, "hour": dt.hour, "text": text, "left": None})
            if len(cues) > MAX_CUES:
                user["cues"] = cues[-MAX_CUES:]

        return self.should_prefarewell(speaker, ts)

    # ------------------------------------------------------------------ #
    # 查詢                                                                 #
    # ------------------------------------------------------------------ #

    def precision(self, speaker: str) -> tuple[int, float]:
        cues = self._data.get(speaker, {}).get("cues", [])
        settled = [c for c in cues if c["left"] is not None]
        n = len(settled)
        if n == 0:
            return 0, 0.0
        hits = sum(1 for c in settled if c["left"])
        return n, hits / n

    def should_prefarewell(self, speaker: str, now: float) -> bool:
        n, p = self.precision(speaker)
        if n < MIN_SAMPLES or p < MIN_PRECISION:
            return False
        last = self._last_farewell.get(speaker, float("-inf"))
        return now - last >= PREFAREWELL_COOLDOWN_S

    def mark_farewelled(self, speaker: str, now: float):
        self._last_farewell[speaker] = now

    def recently_farewelled(self, speaker: str, now: float) -> bool:
        last = self._last_farewell.get(speaker, float("-inf"))
        return now - last < PREFAREWELL_COOLDOWN_S

    # ------------------------------------------------------------------ #
    # 離場結算                                                             #
    # ------------------------------------------------------------------ #

    def on_leave(self, speaker: str, ts: float, persist: bool = True):
        for cue in self._data.get(speaker, {}).get("cues", []):
            if cue["left"] is None and 0 <= ts - cue["ts"] <= CUE_OUTCOME_WINDOW_S:
                cue["left"] = True
        self._expire(speaker, ts)

        buf = self._utter.get(speaker, deque())
        texts = [t for (u_ts, t) in buf if ts - u_ts <= LAST_WORDS_WINDOW_S][-LAST_WORDS_MAX:]
        dt = datetime.fromtimestamp(ts)
        user = self._user(speaker)
        user["last_words"].append({
            "ts": ts, "hour": dt.hour, "weekday": dt.weekday(), "texts": texts,
        })
        if len(user["last_words"]) > MAX_LAST_WORDS:
            user["last_words"] = user["last_words"][-MAX_LAST_WORDS:]
        self._utter[speaker] = deque(maxlen=UTTER_BUFFER_MAX)

        if persist:
            self.save()
