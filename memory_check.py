"""換歌空檔問一句記憶確認（docs/MARVIN_CORE_PRINCIPLES.md 原則 1）。純函式核心。

接線在 cogs/voice_controller_memory_check.py。2026-10-26 使用者定案：
房間長時間沒人講話時，Marvin 在換歌空檔改問一句「你還喜歡 X 嗎？」，取代那次 DJ 口白，
用回答修正記憶。可問歌手、遊戲、食物，輪流問在場者（不要一直問同一個人）。
"""
from __future__ import annotations

import json

from suki_memory import LIKE_THRESHOLD

SILENCE_S = 240.0            # 房間沒人講話多久才問
PER_SPEAKER_GAP_S = 86400.0  # 同一人一天最多問一次
GLOBAL_GAP_S = 1800.0        # 全房間兩次提問至少隔 30 分
ITEM_REASK_S = 30 * 86400.0  # 同一人同一項 30 天內不重問（對齊 taste 衰減寬限）
ANSWER_WINDOW_S = 15.0       # 問完後收回答的秒數
CONFIRM_DELTA = 1.5          # 答「對」加的分（同 daily review 單日幅度）

ASKABLE_CATEGORIES = frozenset({"music", "game", "food"})
_CATEGORIES = ASKABLE_CATEGORIES | {"other"}

CATEGORY_SYSTEM = """把每個「喜好項目」分類成以下其中一類：
music＝歌手、歌曲、音樂類型；game＝電玩或遊戲名稱；food＝食物、飲料、料理、餐廳；
other＝其他一切（興趣、工作、交通工具、人際、價值觀、習慣等）。
只回 JSON 物件，key 是原樣的項目字串，value 是類別。"""

_NO = ("不喜歡", "不太喜歡", "沒有喜歡", "沒在聽", "不聽了", "不是", "沒有", "膩了", "早就不")
_YES_OVERRIDE = ("還不錯", "不錯", "不會不喜歡")  # 含「不」但意思是肯定
_YES = ("喜歡", "對", "是啊", "是阿", "是的", "有啊", "當然", "沒錯", "還好啦喜歡", "超喜歡", "嗯對")

_STRIP_CHARS = "，。！？、,.!?　 \t\n\r"
_MAX_ANSWER_CHARS = 12


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


def room_ready(*, log_rows: list[dict], now: float, last_speech_ts: float) -> bool:
    """房間夠安靜、且距全房間上一次提問夠久。"""
    if now - last_speech_ts < SILENCE_S:
        return False
    asked = [float(r.get("ts") or 0) for r in log_rows if r.get("event") == "asked"]
    if asked and now - max(asked) < GLOBAL_GAP_S:
        return False
    return True


def like_items(taste: dict) -> list[str]:
    return [k for k, v in taste.items()
            if isinstance(v, dict) and float(v.get("score") or 0) >= LIKE_THRESHOLD]


def item_askable(item: str, artists: set[str], categories: dict[str, str]) -> bool:
    """歌手（比對歌手清單）或分類為 music/game/food 的項目才問；分類不出來不問（原則 5）。"""
    return matches_artist(item, artists) or categories.get(item) in ASKABLE_CATEGORIES


def items_needing_category(present: list[str], tastes: dict[str, dict], artists: set[str],
                           categories: dict[str, str]) -> list[str]:
    out: list[str] = []
    for speaker in present:
        for item in like_items(tastes.get(speaker) or {}):
            if item in out or item in categories or matches_artist(item, artists):
                continue
            out.append(item)
    return out


def parse_categories(raw: str | None, items: list[str]) -> dict[str, str]:
    try:
        data = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: (v if v in _CATEGORIES else "other") for k, v in data.items() if k in items}


def _last_asked(rows: list[dict]) -> float:
    return max((float(r.get("ts") or 0) for r in rows), default=0.0)


def pick_question(*, present: list[str], tastes: dict[str, dict], taboos: dict[str, set],
                  artists: set[str], categories: dict[str, str], log_rows: list[dict], now: float,
                  last_speech_ts: float) -> tuple[str, str] | None:
    if not room_ready(log_rows=log_rows, now=now, last_speech_ts=last_speech_ts):
        return None
    asked_rows = [r for r in log_rows if r.get("event") == "asked"]

    # 每位在場者的候選（輪流：先挑最久沒被問的人，再挑他最快被衰減的項目）
    per_person: list[tuple[float, float, int, str, str]] = []
    for order, speaker in enumerate(present):
        taste = tastes.get(speaker)
        if not taste:
            continue
        speaker_asked = [r for r in asked_rows if r.get("speaker") == speaker]
        last_asked = _last_asked(speaker_asked)
        if speaker_asked and now - last_asked < PER_SPEAKER_GAP_S:
            continue
        speaker_taboos = taboos.get(speaker) or set()
        best_item = None
        for item in like_items(taste):
            if item in speaker_taboos or not item_askable(item, artists, categories):
                continue
            item_asked = [r for r in speaker_asked if r.get("item") == item]
            if item_asked and now - _last_asked(item_asked) < ITEM_REASK_S:
                continue
            last_update = float(taste[item].get("last_update") or 0)
            if best_item is None or last_update < best_item[0]:
                best_item = (last_update, item)
        if best_item is not None:
            # 平手：沒人被問過時比誰的項目最快被衰減，再比在場順序
            per_person.append((last_asked, best_item[0], order, speaker, best_item[1]))

    if not per_person:
        return None
    _, _, _, speaker, item = min(per_person)
    return (speaker, item)


def question_text(speaker: str, item: str) -> str:
    return f"{speaker}，你還喜歡{item}嗎？"


def classify_answer(text: str) -> str:
    t = (text or "")
    for ch in _STRIP_CHARS:
        t = t.replace(ch, "")
    # 含喚醒詞＝在下指令；太長＝不是在回答是非題 → 放行給正常流程
    if "馬文" in t or len(t) > _MAX_ANSWER_CHARS:
        return "unclear"
    if any(k in t for k in _YES_OVERRIDE):
        return "yes"
    rest = t
    for k in _NO:
        rest = rest.replace(k, "")
    has_no = rest != t
    has_yes = any(k in rest for k in _YES)  # 「不喜歡」已先挖掉，裡面的「喜歡」不算正面
    if has_no and has_yes:
        return "unclear"  # 正反都有 → 不改記憶（原則 5）
    if has_no:
        return "no"
    if has_yes:
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
