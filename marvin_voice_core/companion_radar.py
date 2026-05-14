"""防呆雷達（Don't-be-dumb Radar）— 規則式 TTS 風險分類器。

設計理念：
    Marvin 即將開口 TTS 之前，用便宜的 keyword/regex 規則檢查
    「這句話在當下脈絡會不會看起來像耍笨」。命中規則時回 risk dict，
    交由 bridge.request_radar_veto 跟 companion 端 user 詢問是否攔下。

    純規則式，無 LLM，無外部 I/O；單一 function (classify_risk) 純函式。
    第一個命中的規則就回傳，不列舉所有 risk。

v1 範圍限制（/review 2026-05-14 確認）：
    cogs/voice_controller.py 的 hook 目前只傳入 atmosphere_snapshot，
    所以實際生產環境只有 _is_tone_mismatch 會觸發。
    _is_defeat_jab 和 _is_sarcasm_to_negative_target 兩條規則已就緒，
    等 Lane F3 在 TTS hook 加入 recent_game_events + target_player + player_memory
    的收集後才會啟用。

context 期望欄位（任何 key 缺失都算「沒資訊」，不會強制要求）：
    - atmosphere_snapshot: dict（含 room_mood）              ← v1 唯一實際傳入的
    - recent_game_events:  list[dict]（each {type, user, ...}） ← Lane F3
    - target_player:       str（被 tease 的人 username）        ← Lane F3
    - player_memory:       dict（target_player 的記憶，含 bias_score）← Lane F3

回傳：
    None 表示安全；dict 表示風險：{rule, reason, severity}
"""

from __future__ import annotations

import re
from typing import Any


# ── Keyword patterns ─────────────────────────────────────────────────────

# defeat keywords：「輸了/輸光/失敗/沒贏/敗了」
# 不加裸字「輸」/「敗」是因為「不輸給」「不會輸」「敗中求勝」之類正面語會誤判
_DEFEAT_PATTERN = re.compile(r"輸了|輸光|失敗|沒贏|敗了|又輸")

# laugh markers：「哈哈/笑死/lol/笑{2,}」
_LAUGH_PATTERN = re.compile(r"哈哈|笑死|lol|LOL|笑笑|笑{2,}", re.IGNORECASE)

# sarcasm markers：「真棒/真聰明/真有趣」
_SARCASM_PATTERN = re.compile(r"真棒|真聰明|真有趣|真厲害|真強")

# 嚴肅氣氛 keywords
_SERIOUS_MOODS = ("認真討論", "嚴肅", "緊繃", "壓力")


# ── Rule functions ──────────────────────────────────────────────────────

def _is_defeat_jab(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """玩家剛輸了，又被吐槽 → defeat_jab。"""
    events = context.get("recent_game_events")
    if not events:
        return None
    # 找最近一筆「lost_round」（或同義）事件
    lost_user = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = ev.get("type", "")
        if t in ("lost_round", "lost", "busted", "timeout"):
            lost_user = ev.get("user") or ev.get("player")
            # 取最新一筆即可
            break
    if not lost_user:
        return None
    if not _DEFEAT_PATTERN.search(text):
        return None
    return {
        "rule": "defeat_jab",
        "reason": f"{lost_user} 剛輸了這輪，再嘲諷可能踩到雷",
        "severity": "medium",
    }


def _is_tone_mismatch(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """嚴肅氣氛開玩笑 → tone_mismatch_serious_to_joke。"""
    atmos = context.get("atmosphere_snapshot")
    if not atmos:
        return None
    mood = (atmos.get("room_mood") or "") if isinstance(atmos, dict) else ""
    if not mood:
        return None
    if not any(s in mood for s in _SERIOUS_MOODS):
        return None
    if not _LAUGH_PATTERN.search(text):
        return None
    return {
        "rule": "tone_mismatch_serious_to_joke",
        "reason": f"房間氣氛「{mood}」，這時候開玩笑可能會冒犯",
        "severity": "medium",
    }


def _is_sarcasm_to_negative_target(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """對 bias 低（< -3）的人講風涼話 → sarcasm_to_negative_bias_target。"""
    target = context.get("target_player")
    memory = context.get("player_memory")
    if not target or not memory:
        return None
    if not isinstance(memory, dict):
        return None
    bias = memory.get("bias_score")
    try:
        bias_val = float(bias) if bias is not None else 0.0
    except (TypeError, ValueError):
        return None
    if bias_val >= -3:
        return None
    if not _SARCASM_PATTERN.search(text):
        return None
    return {
        "rule": "sarcasm_to_negative_bias_target",
        "reason": f"{target} 已經被吐槽過多次（bias {bias_val:.0f}），再 sarcasm 會像欺負",
        "severity": "high",
    }


# 順序：高優先級先（severity high → medium）
_RULES = (
    _is_sarcasm_to_negative_target,
    _is_defeat_jab,
    _is_tone_mismatch,
)


def classify_risk(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """檢查 text 在 context 下是否屬於風險發言。

    回傳第一個命中的 rule dict，或 None。
    """
    if not text or not isinstance(text, str):
        return None
    if context is None:
        return None
    for rule in _RULES:
        try:
            result = rule(text, context)
        except Exception:
            # 任何規則內部例外都不該打斷整體判斷
            continue
        if result is not None:
            return result
    return None
