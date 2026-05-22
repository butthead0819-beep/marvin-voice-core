"""
taste Phase B2：daily review 的 merge_player likes/dislikes 改走 taste 加小分。

問題：原本 merge_player 把 Gemini 給的 likes/dislikes 直接 union 進清單 →「daily 一次
加 11 個 likes」，弱印象一次就變 confirmed，與 feedback loop 的分數模型打架
（feedback_dual_path_taste_writes）。

B2 修法：Gemini 的 likes/dislikes 改成對 taste 加 `_DAILY_TASTE_DELTA`(=1.5) 小分，
新項目只進「曾提及」(taste，分數未達 ±LIKE/DISLIKE_THRESHOLD=±3.0)，累積夠才投影成
confirmed likes/dislikes。existing confirmed（legacy 無 taste）用 _build_taste_from_legacy
保留；結尾 _project_taste 重算。taboos 維持獨立 union（不被分數投影）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from suki_memory import LIKE_THRESHOLD, DISLIKE_THRESHOLD


def _import_module():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


def _empty_player() -> dict:
    return {"likes": [], "dislikes": [], "taboos": [], "taste": {}}


# ── 1. Gemini 新 likes → 弱 taste（曾提及），不直接變 confirmed ────────────────

def test_gemini_likes_become_weak_taste_not_confirmed():
    mod = _import_module()
    merged = mod.merge_player(_empty_player(), {"likes": ["拉麵", "登山", "貓"]})

    assert merged["likes"] == [], "單次 daily 弱印象不該直接變 confirmed like"
    for item in ("拉麵", "登山", "貓"):
        assert item in merged["taste"], f"{item} 應入『曾提及』(taste)"
        assert merged["taste"][item]["score"] == mod._DAILY_TASTE_DELTA


# ── 2. 「11 個 likes 一次塞」→ 0 confirmed，全進曾提及 ────────────────────────

def test_eleven_likes_at_once_yields_zero_confirmed():
    mod = _import_module()
    items = [f"item{i}" for i in range(11)]
    merged = mod.merge_player(_empty_player(), {"likes": items})

    assert merged["likes"] == [], "B2 必須解掉『daily 一次加 11 個 likes』"
    assert len(merged["taste"]) == 11
    assert all(merged["taste"][i]["score"] < LIKE_THRESHOLD for i in items)


# ── 3. existing confirmed（legacy 無 taste）保留 ─────────────────────────────

def test_existing_confirmed_likes_preserved():
    mod = _import_module()
    legacy = {"likes": ["音樂", "電影"], "dislikes": [], "taboos": []}  # 無 taste 欄位
    merged = mod.merge_player(legacy, {})  # Gemini 本輪沒給新 likes

    assert set(merged["likes"]) == {"音樂", "電影"}, "舊 confirmed like 不可消失"
    assert merged["taste"]["音樂"]["score"] >= LIKE_THRESHOLD


# ── 4. 重複提及累積過閾值 → 升級 confirmed ──────────────────────────────────

def test_repeated_mention_promotes_to_confirmed():
    mod = _import_module()
    existing = {
        "likes": [], "dislikes": [], "taboos": [],
        "taste": {"拉麵": {"score": 2.0, "mentions": 1, "first_seen": 0, "last_update": 0}},
    }
    merged = mod.merge_player(existing, {"likes": ["拉麵"]})

    assert merged["taste"]["拉麵"]["score"] == 2.0 + mod._DAILY_TASTE_DELTA
    assert "拉麵" in merged["likes"], "2.0 + 1.5 = 3.5 ≥ 3.0 應升級 confirmed"


# ── 5. dislikes 走負分 ──────────────────────────────────────────────────────

def test_gemini_dislikes_subtract_score():
    mod = _import_module()
    merged = mod.merge_player(_empty_player(), {"dislikes": ["香菜"]})

    assert merged["taste"]["香菜"]["score"] == -mod._DAILY_TASTE_DELTA
    assert merged["dislikes"] == [], "-1.5 > -3.0 → 尚未達 confirmed dislike"


def test_repeated_dislike_promotes_to_confirmed():
    mod = _import_module()
    existing = {
        "likes": [], "dislikes": [], "taboos": [],
        "taste": {"香菜": {"score": -2.0, "mentions": 1, "first_seen": 0, "last_update": 0}},
    }
    merged = mod.merge_player(existing, {"dislikes": ["香菜"]})

    assert merged["taste"]["香菜"]["score"] == -2.0 - mod._DAILY_TASTE_DELTA
    assert "香菜" in merged["dislikes"]
    assert merged["taste"]["香菜"]["score"] <= DISLIKE_THRESHOLD


# ── 6. taboos 維持獨立 union，不進 taste ─────────────────────────────────────

def test_taboos_remain_direct_union_not_taste():
    mod = _import_module()
    existing = {"likes": [], "dislikes": [], "taboos": ["政治"], "taste": {}}
    merged = mod.merge_player(existing, {"taboos": ["宗教"]})

    assert set(merged["taboos"]) == {"政治", "宗教"}
    assert "宗教" not in merged.get("taste", {}), "taboos 是敏感標記，不被分數投影"


# ── 7. mentions 計數遞增（同項目跨日累積）────────────────────────────────────

def test_mentions_incremented_on_repeat():
    mod = _import_module()
    existing = {
        "likes": [], "dislikes": [], "taboos": [],
        "taste": {"咖啡": {"score": 1.5, "mentions": 1, "first_seen": 0, "last_update": 0}},
    }
    merged = mod.merge_player(existing, {"likes": ["咖啡"]})

    assert merged["taste"]["咖啡"]["mentions"] == 2
