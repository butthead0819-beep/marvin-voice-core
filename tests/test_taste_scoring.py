"""TDD: taste 分數分級系統（Phase A）。

把 likes/dislikes 從二元 list 改成分數驅動的信心分級：
  新項目 → 曾提及（taste 內低分）→ 累積 +/- 分 → 過閾值投影到 likes/dislikes
  曾歸類的可因 +/- 分重新調整（like 掉分 → 退回曾提及 / dislike）

likes/dislikes 變成 taste 分數的『自動投影』（保留欄位給現有 consumer）。taboos 維持
獨立（敏感標記，不被分數投影）。Jack 2026-05-22 拍板「保守保留」遷移策略。
"""
from __future__ import annotations

import pytest

from suki_memory import (
    MemoryManager, _repair_player, _build_taste_from_legacy,
    LIKE_THRESHOLD, DISLIKE_THRESHOLD,
)


@pytest.fixture
def mem(tmp_path):
    return MemoryManager(db_path=str(tmp_path / "t.db"),
                         json_compat_path=str(tmp_path / "t.json"))


# ── 曾提及 → 確認 升級 ──────────────────────────────────────────────────────

def test_new_signal_goes_to_mentioned_not_likes(mem):
    """新項目首次提及 → 進 taste（曾提及），低分未過閾值 → 不在 likes。"""
    mem.record_taste_signal("大肚", "拉麵", 1.0)
    p = mem.get_player_memory("大肚")
    assert "拉麵" in p["taste"]
    assert p["taste"]["拉麵"]["score"] == pytest.approx(1.0)
    assert "拉麵" not in p["likes"]


def test_accumulate_promotes_to_likes(mem):
    """累積正分過 LIKE_THRESHOLD → 投影到 likes。"""
    for _ in range(int(LIKE_THRESHOLD)):
        mem.record_taste_signal("大肚", "拉麵", 1.0)
    assert "拉麵" in mem.get_player_memory("大肚")["likes"]


def test_mentions_counter_increments(mem):
    mem.record_taste_signal("大肚", "酒", 1.0)
    mem.record_taste_signal("大肚", "酒", 1.0)
    assert mem.get_player_memory("大肚")["taste"]["酒"]["mentions"] == 2


# ── 動態調整（升降）────────────────────────────────────────────────────────

def test_negative_demotes_from_likes(mem):
    """已是 like 的項目，累積負分掉破閾值 → 退出 likes（曾歸類可重新調整）。"""
    mem.record_taste_signal("大肚", "拉麵", 5.0)
    assert "拉麵" in mem.get_player_memory("大肚")["likes"]
    mem.record_taste_signal("大肚", "拉麵", -3.0)   # 5-3=2 < 3
    assert "拉麵" not in mem.get_player_memory("大肚")["likes"]


def test_strong_negative_goes_to_dislikes(mem):
    mem.record_taste_signal("大肚", "香菜", -3.0)
    assert "香菜" in mem.get_player_memory("大肚")["dislikes"]
    assert "香菜" not in mem.get_player_memory("大肚")["likes"]


def test_score_is_clamped(mem):
    for _ in range(30):
        mem.record_taste_signal("大肚", "酒", 1.0)
    assert mem.get_player_memory("大肚")["taste"]["酒"]["score"] <= 10.0


# ── 遷移（保守保留：舊 likes/dislikes → confirmed）─────────────────────────

def test_repair_migrates_legacy_likes_to_taste():
    """舊資料（無 taste 欄位）載入時 → 從 likes/dislikes 建 taste，分數投影一致。"""
    p = _repair_player({"likes": ["雞排", "酒"], "dislikes": ["香菜"]})
    assert p["taste"]["雞排"]["score"] >= LIKE_THRESHOLD
    assert p["taste"]["香菜"]["score"] <= DISLIKE_THRESHOLD
    # 投影一致：原 likes/dislikes 仍在
    assert "雞排" in p["likes"] and "酒" in p["likes"]
    assert "香菜" in p["dislikes"]


def test_repair_idempotent_does_not_rebuild_after_migration():
    """已有 taste 欄位（即使空）→ 不從 legacy likes 重建（避免清空後復活）。"""
    p = _repair_player({"likes": ["雞排"], "taste": {}})   # taste 已存在但空
    assert p["taste"] == {}            # 不重建
    assert "雞排" not in p["taste"]


def test_build_taste_from_legacy_helper():
    taste = _build_taste_from_legacy(["a"], ["b"])
    assert taste["a"]["score"] >= LIKE_THRESHOLD
    assert taste["b"]["score"] <= DISLIKE_THRESHOLD


# ── remove（移除未確認 / 否定的項目）────────────────────────────────────────

def test_remove_taste_item_clears_taste_and_projection(mem):
    mem.record_taste_signal("weakgogo", "西藏佛學", 5.0)
    assert "西藏佛學" in mem.get_player_memory("weakgogo")["likes"]
    mem.remove_taste_item("weakgogo", "西藏佛學")
    p = mem.get_player_memory("weakgogo")
    assert "西藏佛學" not in p["taste"]
    assert "西藏佛學" not in p["likes"]


# ── taboo 維持獨立（不被分數投影覆蓋）──────────────────────────────────────

def test_taboo_not_overwritten_by_projection(mem):
    mem.mark_taboo("大肚", "手遊詐騙")
    mem.record_taste_signal("大肚", "拉麵", 5.0)   # 觸發投影
    assert "手遊詐騙" in mem.get_player_memory("大肚")["taboos"]
