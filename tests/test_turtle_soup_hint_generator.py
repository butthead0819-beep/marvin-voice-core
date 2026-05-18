"""TDD — hint_generator graph 版：3-layer fallback + schema validation + leak filter。"""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, patch


SURFACE = "男子住在 22 樓..."
TRUTH = "男子是侏儒，按不到 22 樓按鈕。"
KEY_FACTS = ["男子是侏儒", "電梯按鈕高度問題"]
LEAK_KEYWORDS = ["侏儒", "矮", "按鈕"]


def _good_graph():
    return {
        "hint_nodes": [
            {"id": "body_limit", "fact": "主角身體有不尋常的限制"},
            {"id": "tool_reach", "fact": "某些設備在他能力範圍外"},
            {"id": "assist_dep", "fact": "獨自時辦不到、有人在場時可以"},
        ],
        "hints": [
            {"text": "想想他身體上的限制", "reveals": ["body_limit"]},
            {
                "text": "為什麼某些操作有人時可以、自己不行？",
                "reveals": ["body_limit", "tool_reach"],
            },
            {
                "text": "獨自時辦不到 — 這依賴什麼條件？",
                "reveals": ["body_limit", "tool_reach", "assist_dep"],
            },
        ],
    }


# ── 3-layer fallback ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cerebras_succeeds_returns_immediately():
    from game.turtle_soup import hint_generator

    with patch.object(hint_generator, "_call_cerebras",
                      new=AsyncMock(return_value=_good_graph())), \
         patch.object(hint_generator, "_call_groq", new=AsyncMock()) as groq, \
         patch.object(hint_generator, "_call_gemini", new=AsyncMock()) as gemini:
        result = await hint_generator.generate_hint_graph(
            SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS,
        )

    assert result["_provider"] == "Cerebras"
    assert len(result["hint_nodes"]) == 3
    assert len(result["hints"]) == 3
    groq.assert_not_called()
    gemini.assert_not_called()


@pytest.mark.asyncio
async def test_falls_through_to_groq():
    from game.turtle_soup import hint_generator
    with patch.object(hint_generator, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_groq", new=AsyncMock(return_value=_good_graph())), \
         patch.object(hint_generator, "_call_gemini", new=AsyncMock()) as gemini:
        result = await hint_generator.generate_hint_graph(SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS)
    assert result["_provider"] == "Groq"
    gemini.assert_not_called()


@pytest.mark.asyncio
async def test_falls_through_to_gemini():
    from game.turtle_soup import hint_generator
    with patch.object(hint_generator, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_groq", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_gemini", new=AsyncMock(return_value=_good_graph())):
        result = await hint_generator.generate_hint_graph(SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS)
    assert result["_provider"] == "Gemini"


@pytest.mark.asyncio
async def test_all_three_fail_returns_safe_fallback():
    from game.turtle_soup import hint_generator
    with patch.object(hint_generator, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_groq", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_gemini", new=AsyncMock(return_value=None)):
        result = await hint_generator.generate_hint_graph(SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS)
    assert result["_provider"] == "fallback"
    assert result["hint_nodes"] == []
    assert result["hints"] == []


# ── schema validation：嚴格不變式 ─────────────────────────────────────────────

def test_validate_accepts_well_formed():
    from game.turtle_soup.hint_generator import _validate
    assert _validate(_good_graph()) is not None


def test_validate_rejects_too_few_nodes():
    """節點少於 2 個拒絕（沒法形成 graph）。"""
    from game.turtle_soup.hint_generator import _validate
    bad = _good_graph()
    bad["hint_nodes"] = bad["hint_nodes"][:1]
    assert _validate(bad) is None


def test_validate_rejects_too_few_hints():
    from game.turtle_soup.hint_generator import _validate
    bad = _good_graph()
    bad["hints"] = bad["hints"][:1]
    assert _validate(bad) is None


def test_validate_rejects_duplicate_node_ids():
    from game.turtle_soup.hint_generator import _validate
    bad = _good_graph()
    bad["hint_nodes"][1]["id"] = "body_limit"  # 重複 id
    assert _validate(bad) is None


def test_validate_rejects_hint_referencing_undefined_node():
    """hint.reveals 引用不存在的 node id → reject。"""
    from game.turtle_soup.hint_generator import _validate
    bad = _good_graph()
    bad["hints"][0]["reveals"] = ["ghost_node"]
    assert _validate(bad) is None


def test_validate_rejects_non_monotonic_reveals():
    """第 N 條 hint 撤回前一條揭露的節點 → reject。"""
    from game.turtle_soup.hint_generator import _validate
    bad = _good_graph()
    # 把第 2 條改成 reveals=["tool_reach"]（丟掉了第 1 條的 body_limit）
    bad["hints"][1]["reveals"] = ["tool_reach"]
    assert _validate(bad) is None


def test_validate_rejects_no_new_node_in_hint():
    """第 N 條 hint 沒比前一條多揭露任何節點 → reject（重複）。"""
    from game.turtle_soup.hint_generator import _validate
    bad = _good_graph()
    bad["hints"][1]["reveals"] = ["body_limit"]  # 與第 1 條完全一樣
    assert _validate(bad) is None


def test_validate_rejects_empty_reveals():
    from game.turtle_soup.hint_generator import _validate
    bad = _good_graph()
    bad["hints"][0]["reveals"] = []
    assert _validate(bad) is None


def test_validate_rejects_empty_text():
    from game.turtle_soup.hint_generator import _validate
    bad = _good_graph()
    bad["hints"][0]["text"] = ""
    assert _validate(bad) is None


def test_validate_rejects_non_dict():
    from game.turtle_soup.hint_generator import _validate
    assert _validate(None) is None
    assert _validate([]) is None
    assert _validate("string") is None


# ── leak filter ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leak_filter_marks_hint_text_with_keywords():
    from game.turtle_soup import hint_generator
    leaky = _good_graph()
    leaky["hints"][0]["text"] = "他是侏儒"  # 直接洩底
    with patch.object(hint_generator, "_call_cerebras",
                      new=AsyncMock(return_value=leaky)):
        result = await hint_generator.generate_hint_graph(
            SURFACE, TRUTH, KEY_FACTS, ["侏儒"],
        )
    assert "⚠[LEAK" in result["hints"][0]["text"]
    # 後兩條乾淨不該被標記
    assert "⚠" not in result["hints"][1]["text"]


@pytest.mark.asyncio
async def test_leak_filter_does_not_touch_hint_nodes_fact():
    """hint_nodes.fact 是內部欄位，不過濾（玩家看不到）。"""
    from game.turtle_soup import hint_generator
    leaky = _good_graph()
    leaky["hint_nodes"][0]["fact"] = "主角是侏儒"  # 內部 fact 含 leak
    with patch.object(hint_generator, "_call_cerebras",
                      new=AsyncMock(return_value=leaky)):
        result = await hint_generator.generate_hint_graph(
            SURFACE, TRUTH, KEY_FACTS, ["侏儒"],
        )
    # fact 保留原樣（沒加 ⚠）
    assert result["hint_nodes"][0]["fact"] == "主角是侏儒"
