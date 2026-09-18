"""TasteCorrectionAgent — 使用者語音勘誤/查詢 suki_memory 記錯的口味。

用真的 suki_memory.MemoryManager（不自製假記憶類）驗證 taste_forget 只刪「喜歡」側、
不動 taboos/討厭側，以及 taste_query 讀 likes 回報。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext

pytestmark = pytest.mark.asyncio


def _ctx(query: str, mode: str = "normal", speaker: str = "showay", wake_intent: float = 0.9) -> IntentContext:
    return IntentContext(
        speaker=speaker,
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=wake_intent,
        stream_active=(mode == "stream"),
        game_mode=(mode == "game"),
        is_owner=False,
        now=0.0,
        mode=mode,
    )


def _mm(tmp_path):
    import suki_memory
    return suki_memory.MemoryManager(
        db_path=str(tmp_path / "m.db"),
        json_compat_path=str(tmp_path / "m.json"),
    )


def _ctrl(mm):
    ctrl = MagicMock()
    ctrl.speak = AsyncMock()
    ctrl.bot.router.memory = mm
    return ctrl


# ── pattern coverage ─────────────────────────────────────────────────────


@pytest.mark.parametrize("query,expected_item", [
    ("把周杰倫從我的喜好拿掉", "周杰倫"),
    ("我其實沒有喜歡法拉利", "法拉利"),
    ("我沒有很喜歡清酒啦", "清酒"),
    ("忘掉我喜歡臭豆腐", "臭豆腐"),
])
async def test_taste_forget_patterns_match(tmp_path, query, expected_item):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    agent = TasteCorrectionAgent(_ctrl(mm))
    bid = agent.bid(_ctx(query))
    assert bid.confidence > 0.0, f"expected match for {query!r}"
    assert f"taste_forget:{expected_item}" == bid.reason


async def test_taste_query_pattern_matches():
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    ctrl = MagicMock()
    ctrl.speak = AsyncMock()
    agent = TasteCorrectionAgent(ctrl)
    bid = agent.bid(_ctx("你記得我喜歡什麼"))
    assert bid.confidence == 0.85


@pytest.mark.parametrize("query", [
    "我不喜歡香菜",       # 討厭表達，不准命中 forget
    "我沒有很喜歡這首歌",   # 對當下歌曲的回饋（指代詞），不是勘誤
    "我喜歡周杰倫",       # 正面表達喜歡，不是勘誤
    "這首歌我很喜歡",
    "我沒有喜歡你",       # item 只有 1 字
])
async def test_no_match_returns_dense_zero(query):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    ctrl = MagicMock()
    ctrl.speak = AsyncMock()
    agent = TasteCorrectionAgent(ctrl)
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0


async def test_low_wake_gate():
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    ctrl = MagicMock()
    ctrl.speak = AsyncMock()
    agent = TasteCorrectionAgent(ctrl)
    bid = agent.bid(_ctx("把周杰倫從我的喜好拿掉", wake_intent=0.3))
    assert bid.confidence == 0.0
    assert bid.reason == "low_wake"


async def test_game_mode_returns_zero():
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    ctrl = MagicMock()
    ctrl.speak = AsyncMock()
    agent = TasteCorrectionAgent(ctrl)
    bid = agent.bid(_ctx("把周杰倫從我的喜好拿掉", mode="game"))
    assert bid.confidence == 0.0


# ── taste_forget handler ──────────────────────────────────────────────────


async def test_forget_removes_liked_item(tmp_path):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    mm.record_taste_signal("showay", "周杰倫", 5)
    ctrl = _ctrl(mm)
    agent = TasteCorrectionAgent(ctrl)

    bid = agent.bid(_ctx("把周杰倫從我的喜好拿掉"))
    await bid.handler()

    player = mm.get_player_memory("showay")
    assert "周杰倫" not in player.get("taste", {})
    assert "周杰倫" not in (player.get("likes") or [])
    ctrl.speak.assert_awaited_once()
    assert "周杰倫" in ctrl.speak.await_args.args[0]


async def test_forget_substring_match(tmp_path):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    mm.record_taste_signal("showay", "周杰倫的歌", 5)
    ctrl = _ctrl(mm)
    agent = TasteCorrectionAgent(ctrl)

    bid = agent.bid(_ctx("我沒有喜歡周杰倫"))
    await bid.handler()

    player = mm.get_player_memory("showay")
    assert "周杰倫的歌" not in player.get("taste", {})


async def test_forget_does_not_remove_disliked_item(tmp_path):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    mm.record_taste_signal("showay", "香菜", -5)
    ctrl = _ctrl(mm)
    agent = TasteCorrectionAgent(ctrl)

    bid = agent.bid(_ctx("把香菜從我的喜好拿掉"))
    await bid.handler()

    player = mm.get_player_memory("showay")
    assert "香菜" in player.get("taste", {})
    ctrl.speak.assert_awaited_once()
    assert "沒有記得" in ctrl.speak.await_args.args[0]


async def test_forget_does_not_remove_taboo_item(tmp_path):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    mm.record_taste_signal("showay", "家庭", 4)
    mm.mark_taboo("showay", "家庭")
    ctrl = _ctrl(mm)
    agent = TasteCorrectionAgent(ctrl)

    bid = agent.bid(_ctx("把家庭從我的喜好拿掉"))
    await bid.handler()

    player = mm.get_player_memory("showay")
    assert "家庭" in player.get("taste", {})


async def test_forget_no_match_speaks_not_found(tmp_path):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    mm.record_taste_signal("showay", "周杰倫", 5)
    ctrl = _ctrl(mm)
    agent = TasteCorrectionAgent(ctrl)

    bid = agent.bid(_ctx("把五月天從我的喜好拿掉"))
    await bid.handler()

    ctrl.speak.assert_awaited_once()
    assert "五月天" in ctrl.speak.await_args.args[0]
    assert "沒有記得" in ctrl.speak.await_args.args[0]


async def test_forget_unknown_speaker_does_not_create_player(tmp_path):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    ctrl = _ctrl(mm)
    agent = TasteCorrectionAgent(ctrl)

    bid = agent.bid(_ctx("把周杰倫從我的喜好拿掉", speaker="nobody"))
    await bid.handler()

    ctrl.speak.assert_awaited_once()
    assert ctrl.speak.await_args.args[0] == "我本來就沒記得你喜歡什麼。"
    assert mm.has_player("nobody") is False


# ── taste_query handler ────────────────────────────────────────────────────


async def test_query_speaks_likes(tmp_path):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    mm.record_taste_signal("showay", "周杰倫", 5)
    mm.record_taste_signal("showay", "法拉利", 5)
    ctrl = _ctrl(mm)
    agent = TasteCorrectionAgent(ctrl)

    bid = agent.bid(_ctx("你記得我喜歡什麼"))
    await bid.handler()

    ctrl.speak.assert_awaited_once()
    line = ctrl.speak.await_args.args[0]
    assert "周杰倫" in line
    assert "法拉利" in line


async def test_query_no_likes_speaks_fallback(tmp_path):
    from intent_agents.taste_correction_agent import TasteCorrectionAgent
    mm = _mm(tmp_path)
    ctrl = _ctrl(mm)
    agent = TasteCorrectionAgent(ctrl)

    bid = agent.bid(_ctx("我的喜好有哪些"))
    await bid.handler()

    ctrl.speak.assert_awaited_once()
    assert ctrl.speak.await_args.args[0] == "我還沒記下你喜歡什麼。"
