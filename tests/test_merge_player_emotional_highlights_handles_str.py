"""
emotional_highlights merge 防腐：舊資料若被污染成 str（早期版本或手動編輯遺留），
merge_player 不應炸，而是過濾掉非 dict 條目後繼續 dedup/sort。

實際 incident：weakgogo.emotional_highlights = ['焦慮', '挫折感', {dict}, {dict}]，
從 2026-05-24 起 Gemini 給新 highlights → merge 時 _key("焦慮").get() 直接
AttributeError，連續 8 天 daily review 全失敗，suki_memory 停在 5/23。
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


def _player_with_polluted_highlights() -> dict:
    return {
        "likes": [],
        "dislikes": [],
        "taboos": [],
        "taste": {},
        "emotional_highlights": [
            "焦慮",
            "挫折感",
            {"moment": "舊事件", "valence": "annoyed", "timestamp": 1779600000.0},
        ],
    }


def test_merge_skips_str_entries_in_old_highlights():
    """existing 含 str 污染 → merge 不該炸，dict 條目正常保留。"""
    mod = _import_module()
    new_data = {
        "emotional_highlights": [
            {"moment": "新事件", "valence": "happy", "timestamp": 1779700000.0},
        ]
    }
    merged = mod.merge_player(_player_with_polluted_highlights(), new_data)
    eh = merged["emotional_highlights"]
    assert all(isinstance(e, dict) for e in eh), "str 條目應被過濾掉"
    moments = {e["moment"] for e in eh}
    assert "舊事件" in moments
    assert "新事件" in moments


def test_merge_skips_str_entries_in_new_highlights():
    """Gemini 回傳偶發 str 污染 → merge 不該炸。"""
    mod = _import_module()
    existing = {
        "likes": [], "dislikes": [], "taboos": [], "taste": {},
        "emotional_highlights": [
            {"moment": "舊事件", "valence": "annoyed", "timestamp": 1779600000.0},
        ],
    }
    new_data = {
        "emotional_highlights": [
            "孤獨",  # Gemini 偶發給裸字串
            {"moment": "新事件", "valence": "happy", "timestamp": 1779700000.0},
        ]
    }
    merged = mod.merge_player(existing, new_data)
    eh = merged["emotional_highlights"]
    assert all(isinstance(e, dict) for e in eh)
    moments = {e["moment"] for e in eh}
    assert moments == {"舊事件", "新事件"}


def test_merge_dedup_still_works_after_filtering():
    """過濾 str 後，dict 的 dedup by (moment, timestamp) 仍正常。"""
    mod = _import_module()
    existing = {
        "likes": [], "dislikes": [], "taboos": [], "taste": {},
        "emotional_highlights": [
            "污染1",
            {"moment": "重複事件", "valence": "neutral", "timestamp": 1779600000.0},
        ],
    }
    new_data = {
        "emotional_highlights": [
            {"moment": "重複事件", "valence": "neutral", "timestamp": 1779600000.0},
            {"moment": "新事件", "valence": "happy", "timestamp": 1779700000.0},
        ]
    }
    merged = mod.merge_player(existing, new_data)
    eh = merged["emotional_highlights"]
    moments = [e["moment"] for e in eh]
    assert moments.count("重複事件") == 1
    assert "新事件" in moments
