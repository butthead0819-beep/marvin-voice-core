"""換歌空檔問一句記憶確認（docs/MARVIN_CORE_PRINCIPLES.md 原則 1）。純函式核心。

接線在 cogs/voice_controller_memory_check.py。2026-10-26 使用者定案：
房間長時間沒人講話時，Marvin 在換歌空檔改問一句「你還喜歡 X 嗎？」，取代那次 DJ 口白，
用回答修正記憶。v1 只問口味（歌手類），多人在場也可以問。
"""
from __future__ import annotations

from suki_memory import LIKE_THRESHOLD

SILENCE_S = 240.0            # 房間沒人講話多久才問
PER_SPEAKER_GAP_S = 86400.0  # 同一人一天最多問一次
GLOBAL_GAP_S = 1800.0        # 全房間兩次提問至少隔 30 分
ITEM_REASK_S = 30 * 86400.0  # 同一人同一項 30 天內不重問（對齊 taste 衰減寬限）
ANSWER_WINDOW_S = 15.0       # 問完後收回答的秒數
CONFIRM_DELTA = 1.5          # 答「對」加的分（同 daily review 單日幅度）

_NO = ("不喜歡", "不太喜歡", "沒有喜歡", "沒在聽", "不聽了", "不是", "沒有", "膩了", "早就不")
_YES_OVERRIDE = ("還不錯", "不錯", "不會不喜歡")  # 含「不」但意思是肯定
_YES = ("喜歡", "對", "是啊", "是阿", "是的", "有啊", "當然", "沒錯", "還好啦喜歡", "超喜歡", "嗯對")

_STRIP_CHARS = "，。！？、,.!?　 \t\n\r"


def artist_names(fingerprint: dict) -> set[str]:
    """從 taste_fingerprint 結構取歌手名（core_artists ∪ 每人 per_user.core_artists）。

    結構缺/壞 → 空集合，不 raise。"""
    out: set[str] = set()
    if not isinstance(fingerprint, dict):
        return out
    try:
        for a in fingerprint.get("core_artists") or []:
            if isinstance(a, (list, tuple)) and a and a[0]:
                out.add(a[0])
        for u in (fingerprint.get("per_user") or {}).values():
            if not isinstance(u, dict):
                continue
            for a in u.get("core_artists") or []:
                if isinstance(a, (list, tuple)) and a and a[0]:
                    out.add(a[0])
    except Exception:
        return set()
    return out


def matches_artist(item: str, artists: set[str]) -> bool:
    """item 與任一 artist：完全相等，或兩者長度都 ≥2 且互為子字串。"""
    if not item:
        return False
    for a in artists:
        if not a:
            continue
        if item == a:
            return True
        if len(item) >= 2 and len(a) >= 2 and (item in a or a in item):
            return True
    return False


def pick_question(*, present: list[str], tastes: dict[str, dict], taboos: dict[str, set],
                   artists: set[str], log_rows: list[dict], now: float,
                   last_speech_ts: float) -> tuple[str, str] | None:
    if now - last_speech_ts < SILENCE_S:
        return None

    asked_rows = [r for r in log_rows if r.get("event") == "asked"]
    if asked_rows:
        latest_ts = max(float(r.get("ts") or 0) for r in asked_rows)
        if now - latest_ts < GLOBAL_GAP_S:
            return None

    candidates: list[tuple[str, str, float]] = []
    for speaker in present:
        taste = tastes.get(speaker)
        if not taste:
            continue

        speaker_asked = [r for r in asked_rows if r.get("speaker") == speaker]
        if speaker_asked:
            last_speaker_ts = max(float(r.get("ts") or 0) for r in speaker_asked)
            if now - last_speaker_ts < PER_SPEAKER_GAP_S:
                continue

        speaker_taboos = taboos.get(speaker) or set()
        for item, entry in taste.items():
            if not isinstance(entry, dict):
                continue
            score = float(entry.get("score") or 0)
            if score < LIKE_THRESHOLD:
                continue
            if item in speaker_taboos:
                continue
            if not matches_artist(item, artists):
                continue

            item_asked = [r for r in speaker_asked if r.get("item") == item]
            if item_asked:
                last_item_ts = max(float(r.get("ts") or 0) for r in item_asked)
                if now - last_item_ts < ITEM_REASK_S:
                    continue

            last_update = float(entry.get("last_update") or 0)
            candidates.append((speaker, item, last_update))

    if not candidates:
        return None

    best = min(candidates, key=lambda c: c[2])
    return (best[0], best[1])


def question_text(speaker: str, item: str) -> str:
    return f"{speaker}，你還喜歡{item}嗎？"


def classify_answer(text: str) -> str:
    t = (text or "")
    for ch in _STRIP_CHARS:
        t = t.replace(ch, "")
    if any(k in t for k in _YES_OVERRIDE):
        return "yes"
    if any(k in t for k in _NO):
        return "no"
    if any(k in t for k in _YES):
        return "yes"
    return "unclear"


def answer_window_open(window: dict | None, speaker: str, now: float) -> bool:
    if not window:
        return False
    if window.get("speaker") != speaker:
        return False
    if now > float(window.get("deadline") or 0):
        return False
    return True
