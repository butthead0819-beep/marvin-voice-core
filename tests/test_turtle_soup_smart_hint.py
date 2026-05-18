"""TDD — engine 個人化 hint 排序：

- 線性 puzzle 維持原順序（向下相容）
- 分支 puzzle：選 new_nodes 最少的 hint
- 玩家問題 keyword 命中節點 → 引擎跳過已探索節點的 hint
- 用完所有 hint 回 None
"""
from __future__ import annotations
import uuid
import pytest
from unittest.mock import AsyncMock, patch

from game.turtle_soup.session import (
    AskedQuestion,
    TurtleSoupSession,
    TurtleSoupState,
)
from game.turtle_soup.puzzles import Hint, HintNode, Puzzle
from game.turtle_soup.engine import TurtleSoupEngine


def _stub_cb():
    return AsyncMock()


def _new_session():
    return TurtleSoupSession(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
    )


async def _setup_in_asking(puzzle: Puzzle) -> TurtleSoupEngine:
    eng = TurtleSoupEngine(session=_new_session(), puzzle=puzzle, on_state_change=_stub_cb())
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()
    return eng


# ── 線性 puzzle（ELEVATOR_18F-style）─────────────────────────────────────────

LINEAR_PUZZLE = Puzzle(
    id="linear_test",
    surface="surface",
    truth="truth",
    hint_nodes=[
        HintNode(id="A", fact="A fact", keywords=("akey",)),
        HintNode(id="B", fact="B fact", keywords=("bkey",)),
        HintNode(id="C", fact="C fact", keywords=("ckey",)),
    ],
    hints=[
        Hint(text="hint_A", reveals=("A",)),
        Hint(text="hint_AB", reveals=("A", "B")),
        Hint(text="hint_ABC", reveals=("A", "B", "C")),
    ],
)


@pytest.mark.asyncio
async def test_linear_puzzle_preserves_original_order():
    """線性 puzzle 連續 request 應依 hints[0], [1], [2] 順序給。"""
    eng = await _setup_in_asking(LINEAR_PUZZLE)
    assert await eng.request_hint() == "hint_A"
    assert await eng.request_hint() == "hint_AB"
    assert await eng.request_hint() == "hint_ABC"
    assert await eng.request_hint() is None


# ── 玩家問題 keyword 命中 → 跳過已探索 hint ─────────────────────────────────

@pytest.mark.asyncio
async def test_question_keyword_hits_node_skips_redundant_hint():
    """玩家問「他身高有問題嗎」→ A 已探索 → 下一條應跳過 hint_A，直接給 hint_AB。"""
    import time
    eng = await _setup_in_asking(LINEAR_PUZZLE)

    # 模擬玩家已問了一個含 'akey' 關鍵詞的問題
    eng.session.asked_questions.append(AskedQuestion(
        asker_id="u1", asker_name="Alice",
        question="這跟 akey 有關嗎？",
        verdict="yes", narration="嗯", provider="Cerebras", timestamp=time.time(),
    ))

    # 第一條 hint 應是 hint_AB（因為 A 已探索，hint_A 沒新資訊）
    first = await eng.request_hint()
    assert first == "hint_AB"

    # 再問下一條，B 也被 hint_AB 揭露了 → 應給 hint_ABC
    second = await eng.request_hint()
    assert second == "hint_ABC"


@pytest.mark.asyncio
async def test_player_explores_all_nodes_via_questions_no_hint_available():
    """玩家問題已 cover 所有節點 keyword → 沒可給的 hint。"""
    import time
    eng = await _setup_in_asking(LINEAR_PUZZLE)

    for kw in ("akey", "bkey", "ckey"):
        eng.session.asked_questions.append(AskedQuestion(
            asker_id="u1", asker_name="Alice",
            question=f"關於 {kw} 的問題",
            verdict="yes", narration="ok", provider="Cerebras", timestamp=time.time(),
        ))

    assert await eng.request_hint() is None


# ── 分支 puzzle：選 new_nodes 最少 ──────────────────────────────────────────

BRANCH_PUZZLE = Puzzle(
    id="branch_test",
    surface="s",
    truth="t",
    hint_nodes=[
        HintNode(id="x", fact="x"),
        HintNode(id="y", fact="y"),
        HintNode(id="z", fact="z"),
    ],
    hints=[
        # 三條獨立 hint，各只揭露 1 個節點（branches）
        Hint(text="hint_x", reveals=("x",)),
        Hint(text="hint_y", reveals=("y",)),
        Hint(text="hint_z", reveals=("z",)),
        # 一條總結 hint，揭露所有
        Hint(text="hint_xyz", reveals=("x", "y", "z")),
    ],
)


@pytest.mark.asyncio
async def test_branch_puzzle_picks_smallest_new_nodes_first():
    """分支 puzzle 第一次 request 應拿 1-node hint，不該直接給 3-node summary。"""
    eng = await _setup_in_asking(BRANCH_PUZZLE)
    first = await eng.request_hint()
    # 應該是 hint_x, hint_y, hint_z 之一（都 1 node），不應是 hint_xyz
    assert first in ("hint_x", "hint_y", "hint_z")
    assert first != "hint_xyz"


@pytest.mark.asyncio
async def test_branch_puzzle_skips_subsumed_summary():
    """已給 x、y、z 三條後，summary hint 沒新資訊 → 應 return None。"""
    eng = await _setup_in_asking(BRANCH_PUZZLE)
    given = []
    for _ in range(3):
        h = await eng.request_hint()
        given.append(h)
    # 應該都是 1-node hint
    assert set(given) == {"hint_x", "hint_y", "hint_z"}
    # 第 4 次 → summary 沒新資訊（all explored）→ None
    assert await eng.request_hint() is None


# ── tie-break：同 new_nodes 數選 reveals 最小 ────────────────────────────────

TIE_PUZZLE = Puzzle(
    id="tie_test",
    surface="s",
    truth="t",
    hint_nodes=[
        HintNode(id="m", fact="m"),
        HintNode(id="n", fact="n"),
    ],
    hints=[
        Hint(text="combo", reveals=("m", "n")),     # 2 nodes
        Hint(text="single", reveals=("m",)),        # 1 node
    ],
)


@pytest.mark.asyncio
async def test_tie_break_prefers_smaller_reveals():
    """同樣 new_nodes 內含時，hint 越少 reveal 越好（更乾淨的線索）。

    這裡 single 揭露 {m} 是 combo 揭露 {m,n} 的子集。
    第一次 request 應該選 single（new=1, reveals=1），不選 combo（new=2, reveals=2）。
    """
    eng = await _setup_in_asking(TIE_PUZZLE)
    first = await eng.request_hint()
    assert first == "single"
    second = await eng.request_hint()
    assert second == "combo"


# ── given_hint_indices 紀錄 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_given_hint_indices_tracks_what_was_given():
    eng = await _setup_in_asking(LINEAR_PUZZLE)
    await eng.request_hint()
    await eng.request_hint()
    assert eng.session.given_hint_indices == [0, 1]


@pytest.mark.asyncio
async def test_same_hint_not_given_twice():
    """每條 hint 只給一次（透過 given_hint_indices 防重複）。"""
    eng = await _setup_in_asking(LINEAR_PUZZLE)
    first = await eng.request_hint()
    second = await eng.request_hint()
    third = await eng.request_hint()
    assert first != second
    assert second != third
    assert first != third


# ── _explored_node_ids 單元測試 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_explored_includes_revealed_from_given_hints():
    eng = await _setup_in_asking(LINEAR_PUZZLE)
    eng.session.given_hint_indices.append(0)  # hint_A 揭露 A
    explored = eng._explored_node_ids()
    assert "A" in explored


@pytest.mark.asyncio
async def test_explored_includes_keyword_matched_from_questions():
    import time
    eng = await _setup_in_asking(LINEAR_PUZZLE)
    eng.session.asked_questions.append(AskedQuestion(
        asker_id="u1", asker_name="Alice",
        question="bkey 在哪？",
        verdict="no", narration="x", provider="Cerebras", timestamp=time.time(),
    ))
    explored = eng._explored_node_ids()
    assert "B" in explored
