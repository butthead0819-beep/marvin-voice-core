"""
Per-player merge 隔離：一個玩家的 merge 炸了，不能拖垮其他玩家。

2026-05-24 incident 模式：weakgogo 的 emotional_highlights 含 str → merge_player
raise AttributeError → 整個 for 迴圈中止 → merged_players 不完整 → suki_memory 不
寫入。已修 emotional_highlights 但其他欄位（personal_info / behavioral_patterns /
speech_dna / stats）同樣可能撞 schema mismatch。

修法：merge_player 抽出 merge_players_safe()，per-player 包 try/except，
失敗時 log warning + 保留 existing 不變，其他玩家照常 merge。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _import_module():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


def _player(name: str = "X") -> dict:
    return {"likes": [], "dislikes": [], "taboos": [], "taste": {}}


# ── 1. 一個玩家炸，其他玩家照常 merge ─────────────────────────────────────────

def test_one_player_explodes_others_still_merge():
    """weakgogo 含污染（speech_dna 是 list 而非 dict）→ merge_player 炸，
    其他玩家仍正常 merge。
    """
    mod = _import_module()
    existing = {
        "weakgogo": {**_player("weakgogo"), "speech_dna": ["bad", "list"]},  # ← 污染
        "showay":   _player("showay"),
        "狗與露":   _player("狗與露"),
    }
    updated = {
        "weakgogo": {"speech_dna": {"style_summary": "casual"}},   # ← dict(list) 炸
        "showay":   {"likes": ["拉麵"]},
        "狗與露":   {"likes": ["登山"]},
    }
    merged = mod.merge_players_safe(existing, updated)

    # weakgogo 保留 existing（沒被毀）
    assert merged["weakgogo"]["speech_dna"] == ["bad", "list"], \
        "炸的玩家應保留 existing，不被破壞"
    # 其他玩家照常 merge
    assert "拉麵" in merged["showay"]["taste"]
    assert "登山" in merged["狗與露"]["taste"]


# ── 2. 全部正常 → 行為與直接呼叫 merge_player 等價 ────────────────────────────

def test_all_players_merge_normally():
    mod = _import_module()
    existing = {
        "showay": _player("showay"),
        "weakgogo": _player("weakgogo"),
    }
    updated = {
        "showay":   {"likes": ["拉麵"]},
        "weakgogo": {"likes": ["登山"]},
    }
    merged = mod.merge_players_safe(existing, updated)
    assert "拉麵" in merged["showay"]["taste"]
    assert "登山" in merged["weakgogo"]["taste"]


# ── 3. 新玩家（existing 沒有）直接寫入，不走 merge ───────────────────────────

def test_new_player_added_as_is():
    mod = _import_module()
    existing = {"showay": _player("showay")}
    updated = {
        "showay":  {"likes": ["拉麵"]},
        "新玩家": {"likes": ["游泳"], "taboos": ["某禁忌"]},   # 不存在 existing
    }
    merged = mod.merge_players_safe(existing, updated)
    assert "拉麵" in merged["showay"]["taste"]
    assert merged["新玩家"]["likes"] == ["游泳"]
    assert merged["新玩家"]["taboos"] == ["某禁忌"]


# ── 4. 多個玩家都炸 → 每個都被隔離 ──────────────────────────────────────────

def test_multiple_players_explode_each_isolated():
    mod = _import_module()
    existing = {
        "weakgogo":  {**_player(), "speech_dna": ["bad"]},
        "showay":    {**_player(), "behavioral_patterns": "bad_str"},
        "狗與露":    _player("狗與露"),
    }
    updated = {
        "weakgogo":  {"speech_dna": {"x": "y"}},
        "showay":    {"behavioral_patterns": {"y": "z"}},
        "狗與露":    {"likes": ["登山"]},
    }
    merged = mod.merge_players_safe(existing, updated)
    # 兩個炸的都保留 existing
    assert merged["weakgogo"]["speech_dna"] == ["bad"]
    assert merged["showay"]["behavioral_patterns"] == "bad_str"
    # 正常的照常 merge
    assert "登山" in merged["狗與露"]["taste"]
