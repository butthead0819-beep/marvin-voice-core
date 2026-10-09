"""TDD — dj_topic_selector.select_mode 扭蛋池：純函式層驗證。

用 random.Random(seed) 注入，結果可重現、不依賴全域 random 狀態、不用 mock 產品邏輯
自己驗算自己——直接呼叫真正的 select_mode，斷言其輸出分布/邊界行為。
"""
from __future__ import annotations

import random

import dj_topic_selector
from dj_life_context import LifeCore
from dj_topic_selector import TopicCooldownStore, select_mode

ALL_MODES = (
    "life", "interest", "emotional_highlight", "news",
    "guide", "conversation", "atmosphere", "quick",
)


def _store(tmp_path):
    return TopicCooldownStore(str(tmp_path / "c.json"))


# 1. 全素材都有時，連抽 300 次：7 種會走 LLM 的 mode 都出現、quick 從不出現 ─────

def test_all_llm_modes_appear_and_quick_never_when_material_rich(tmp_path):
    store = _store(tmp_path)
    rng = random.Random(42)
    seen = set()
    for i in range(300):
        _, mode = select_mode(
            [f"life{i}"], [f"interest{i}"], store,
            has_conversation=True, has_guide=True,
            emotional_highlights=[f"emo{i}"], news_items=[f"news{i}"],
            rng=rng,
        )
        seen.add(mode)
    assert seen == set(ALL_MODES) - {"quick"}


# 1b. quick（本地模板，聽起來像保底口白）只在沒別的可抽時墊底 ─────────────────

def test_quick_not_drawn_while_other_mode_available(tmp_path):
    store = _store(tmp_path)
    store.set_last_fallback("atmosphere")
    for seed in range(30):
        _, mode = select_mode([], [], store, has_conversation=True, rng=random.Random(seed))
        assert mode == "conversation"
        store.set_last_fallback("atmosphere")


def test_quick_is_fallback_when_only_atmosphere_was_just_used(tmp_path):
    store = _store(tmp_path)
    store.set_last_fallback("atmosphere")
    _, mode = select_mode([], [], store, rng=random.Random(0))
    assert mode == "quick"


def test_atmosphere_after_quick_when_no_material(tmp_path):
    store = _store(tmp_path)
    store.set_last_fallback("quick")
    _, mode = select_mode([], [], store, rng=random.Random(0))
    assert mode == "atmosphere"


# 2. 連抽 300 次，相鄰兩次 mode 永遠不同（不連抽）───────────────────────────

def test_adjacent_draws_never_repeat(tmp_path):
    store = _store(tmp_path)
    rng = random.Random(7)
    prev = None
    for i in range(300):
        _, mode = select_mode(
            [f"life{i}"], [f"interest{i}"], store,
            has_conversation=True, has_guide=True,
            emotional_highlights=[f"emo{i}"], news_items=[f"news{i}"],
            rng=rng,
        )
        if prev is not None:
            assert mode != prev
        prev = mode


# 3. 池只剩 1 個 mode 時可以連續抽同一個 ────────────────────────────────────

def test_single_mode_pool_can_repeat_consecutively(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"guide": 1.0})
    store = _store(tmp_path)
    rng = random.Random(1)
    modes = [select_mode([], [], store, has_guide=True, rng=rng)[1] for _ in range(20)]
    assert all(m == "guide" for m in modes)


# 4. 沒素材的 mode 永不出現 ─────────────────────────────────────────────────

def test_modes_without_material_never_appear(tmp_path):
    store = _store(tmp_path)
    life = [LifeCore("有主角的生活事件", speakers=("不在場的人",))]
    store.mark_used("已冷卻的興趣")
    rng = random.Random(5)
    for _ in range(200):
        _, mode = select_mode(
            life, ["已冷卻的興趣"], store,
            present_members={"在場的人"},
            has_conversation=False, has_guide=False,
            rng=rng,
        )
        assert mode not in ("guide", "conversation", "life", "interest")


# 5. 權重 0 的 mode 永不出現 ────────────────────────────────────────────────

def test_zero_weight_mode_never_appears(tmp_path, monkeypatch):
    weights = dict(dj_topic_selector.MODE_WEIGHTS)
    weights["life"] = 0.0
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", weights)
    store = _store(tmp_path)
    rng = random.Random(9)
    for i in range(100):
        _, mode = select_mode(
            [f"life{i}"], [], store, has_conversation=True,
            rng=rng,
        )
        assert mode != "life"


# 6. 沒被抽中的候選話題不會被白白冷卻 ───────────────────────────────────────

def test_unchosen_candidate_topic_not_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"interest": 1.0})
    store = _store(tmp_path)
    topic, mode = select_mode(["生活話題"], ["興趣話題"], store)
    assert mode == "interest"
    assert topic == "興趣話題"
    assert store.is_cool("生活話題") is True
    assert store.is_cool("興趣話題") is False


# 7. topic_text：話題類回素材文字，非話題類回 None ─────────────────────────

def test_topic_text_none_for_non_topic_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"quick": 1.0})
    store = _store(tmp_path)
    topic, mode = select_mode([], [], store)
    assert mode == "quick"
    assert topic is None


def test_topic_text_non_none_for_topic_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"life": 1.0})
    store = _store(tmp_path)
    topic, mode = select_mode(["生活話題"], [], store)
    assert mode == "life"
    assert topic == "生活話題"


# 8. activity（10/8 使用者定案：在場者的 Discord 正在玩/自訂狀態）───────────

def test_activity_in_pool_and_drawn_marks_used(tmp_path):
    store = _store(tmp_path)
    store.set_last_fallback("atmosphere")
    rng = random.Random(42)

    topic, mode = select_mode(
        [], [], store,
        activities=["小明 正在玩《Ball X Pit》"],
        rng=rng,
    )
    assert mode == "activity"
    assert topic == "小明 正在玩《Ball X Pit》"
    assert store.is_cool("小明 正在玩《Ball X Pit》") is False


def test_activity_cooldown_excludes_it_from_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"activity": 1.0, "quick": 1.0})
    store = _store(tmp_path)
    store.mark_used("小明 正在玩《Ball X Pit》")
    topic, mode = select_mode([], [], store, activities=["小明 正在玩《Ball X Pit》"])
    assert mode != "activity"
    assert topic is None


def test_no_activities_passed_behaves_like_before(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"activity": 1.0, "quick": 1.0})
    store = _store(tmp_path)
    topic, mode = select_mode([], [], store)
    assert mode == "quick"
    assert topic is None
