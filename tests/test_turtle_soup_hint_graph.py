"""TDD — hint graph 資料模型不變式。

驗證：
- HintNode 是 atomic insight
- Hint.reveals 引用的 node id 都存在
- 後一條 hint reveals 包含前一條的 superset（遞進不撤回）
- ELEVATOR_18F 的網結構符合上述
"""
from __future__ import annotations
import pytest

from game.turtle_soup.puzzles import (
    ELEVATOR_18F,
    Hint,
    HintNode,
    Puzzle,
    get_default_puzzle,
)


# ── HintNode / Hint dataclass 行為 ──────────────────────────────────────────

def test_hint_node_is_frozen():
    n = HintNode(id="x", fact="y")
    with pytest.raises(Exception):
        n.id = "z"  # frozen 應 raise


def test_hint_is_frozen_and_reveals_is_tuple():
    h = Hint(text="t", reveals=("a", "b"))
    assert h.reveals == ("a", "b")
    assert isinstance(h.reveals, tuple)


def test_hint_default_reveals_empty_tuple():
    h = Hint(text="bare")
    assert h.reveals == ()


# ── Puzzle.hint_node_by_id ──────────────────────────────────────────────────

def test_hint_node_by_id_finds_existing():
    node = ELEVATOR_18F.hint_node_by_id("body_limit")
    assert node is not None
    assert "身體" in node.fact


def test_hint_node_by_id_missing_returns_none():
    assert ELEVATOR_18F.hint_node_by_id("nonexistent") is None


# ── 不變式：reveals 引用的 node 必須存在 ──────────────────────────────────────

def _all_node_ids(puzzle: Puzzle) -> set[str]:
    return {n.id for n in puzzle.hint_nodes}


def test_all_hints_reveals_reference_valid_nodes():
    """每條 hint.reveals 提到的 id 都要在 hint_nodes 裡定義過。"""
    node_ids = _all_node_ids(ELEVATOR_18F)
    for hint in ELEVATOR_18F.hints:
        for nid in hint.reveals:
            assert nid in node_ids, (
                f"Hint {hint.text!r} 引用了未定義的 node id {nid!r}"
            )


# ── 不變式：後一條 hint reveals 是前一條的 superset（遞進不撤回）─────────────

def test_hints_are_monotonically_revealing():
    """玩家拿到第 N 條 hint 時，第 N-1 條的內容應該還包含在 reveals 裡。

    這保證 hint 是「逐步揭開」而非「跳來跳去」，符合『編織網但有方向』的設計。
    """
    prev_reveals: set[str] = set()
    for i, hint in enumerate(ELEVATOR_18F.hints):
        current = set(hint.reveals)
        assert prev_reveals.issubset(current), (
            f"Hint #{i} ({hint.text!r}) 撤回了之前揭露的節點："
            f"prev={prev_reveals}, current={current}"
        )
        prev_reveals = current


def test_hints_strictly_advance_in_depth():
    """每條 hint 至少揭露一個前一條沒揭露的節點（避免重複）。"""
    prev_reveals: set[str] = set()
    for i, hint in enumerate(ELEVATOR_18F.hints):
        current = set(hint.reveals)
        new_nodes = current - prev_reveals
        assert new_nodes, (
            f"Hint #{i} ({hint.text!r}) 沒揭露任何新節點，與前一條重複"
        )
        prev_reveals = current


# ── ELEVATOR_18F 結構檢查 ───────────────────────────────────────────────────

def test_elevator_18f_has_three_nodes():
    """v0 設計：核心推理鏈長度 3。"""
    assert len(ELEVATOR_18F.hint_nodes) == 3


def test_elevator_18f_has_three_hints():
    assert len(ELEVATOR_18F.hints) == 3


def test_elevator_18f_hints_progress_1_2_3_nodes():
    """ELEVATOR_18F 的三條 hint 揭露數量是 1, 2, 3（線性遞進）。"""
    assert len(ELEVATOR_18F.hints[0].reveals) == 1
    assert len(ELEVATOR_18F.hints[1].reveals) == 2
    assert len(ELEVATOR_18F.hints[2].reveals) == 3


def test_default_puzzle_is_elevator():
    assert get_default_puzzle() is ELEVATOR_18F
