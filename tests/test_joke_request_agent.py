"""JokeRequestAgent — 喚醒後直接「馬文說個笑話」→ 本地 joke bank 抽一則念出來。

2026-08-31 daily ritual：showay 兩小時內問了兩次「馬文說個笑話來聽聽」，兩次落到
不同 intent_type（social_joke_request / social_talk_request）、都沒 agent，第一次被
模板 ack、第二次連 ack 都沒有。決策（Jack 拍板）：複用 DJ 的 joke_bank 泛用池抽，
零 LLM（中文諧音笑話 LLM 現編是能力斷崖，見 memory project_dj_joke_bank_pinyin_match）。

confidence 0.85，1 個 intent：joke_request。mode_compatible = {"normal", "stream"}。
Handler：joke_bank.random_joke(exclude=近期) → ctrl.speak。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext

pytestmark = pytest.mark.asyncio


def _ctx(query: str, mode: str = "normal") -> IntentContext:
    return IntentContext(
        speaker="showay",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=(mode == "stream"),
        game_mode=(mode == "game"),
        is_owner=False,
        now=0.0,
        mode=mode,
    )


def _ctrl() -> MagicMock:
    ctrl = MagicMock()
    ctrl.speak = AsyncMock()
    return ctrl


# ── pattern coverage ─────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "說個笑話來聽聽",
    "說笑話來聽聽",
    "講個笑話",
    "講笑話",
    "說一個笑話",
    "講則笑話",
    "來個笑話",
    "來點笑話",
    "說個冷笑話",
])
async def test_joke_request_patterns(query):
    from intent_agents.joke_request_agent import JokeRequestAgent
    agent = JokeRequestAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.85, f"expected 0.85 for {query!r}, got {bid.confidence}"


@pytest.mark.parametrize("query", [
    "今天天氣不錯",
    "播放周杰倫",
    "這個笑話很好笑",   # 「笑話」出現但不是在要笑話
    "下一首",
])
async def test_no_match_returns_dense_zero(query):
    from intent_agents.joke_request_agent import JokeRequestAgent
    agent = JokeRequestAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0


async def test_game_mode_returns_mode_mismatch():
    from intent_agents.joke_request_agent import JokeRequestAgent
    agent = JokeRequestAgent(_ctrl())
    bid = agent.bid(_ctx("說個笑話", mode="game"))
    assert bid.confidence == 0.0
    assert "mode_mismatch" in bid.reason


async def test_matches_in_stream_mode():
    from intent_agents.joke_request_agent import JokeRequestAgent
    agent = JokeRequestAgent(_ctrl())
    bid = agent.bid(_ctx("說個笑話", mode="stream"))
    assert bid.confidence == 0.85


# ── handler ──────────────────────────────────────────────────────────────


async def test_handler_speaks_a_joke(monkeypatch):
    from intent_agents import joke_request_agent

    fake_bank = MagicMock()
    fake_bank.random_joke.return_value = "稻草人的笑話。……付出跟頭銜不成正比。"
    monkeypatch.setattr(joke_request_agent, "get_joke_bank", lambda: fake_bank)

    ctrl = _ctrl()
    agent = joke_request_agent.JokeRequestAgent(ctrl)
    bid = agent.bid(_ctx("說個笑話"))
    await bid.handler()

    ctrl.speak.assert_awaited_once()
    assert ctrl.speak.await_args.args[0] == "稻草人的笑話。……付出跟頭銜不成正比。"


async def test_handler_avoids_recent_repeats(monkeypatch):
    from intent_agents import joke_request_agent

    fake_bank = MagicMock()
    fake_bank.random_joke.return_value = "笑話 A"
    monkeypatch.setattr(joke_request_agent, "get_joke_bank", lambda: fake_bank)

    ctrl = _ctrl()
    agent = joke_request_agent.JokeRequestAgent(ctrl)
    await agent.bid(_ctx("說個笑話")).handler()

    # 第二次要求：上一則要在 exclude 裡
    await agent.bid(_ctx("說個笑話")).handler()
    _, kwargs = fake_bank.random_joke.call_args
    assert "笑話 A" in kwargs["exclude"]


async def test_handler_empty_bank_does_not_crash(monkeypatch):
    from intent_agents import joke_request_agent

    fake_bank = MagicMock()
    fake_bank.random_joke.return_value = None
    monkeypatch.setattr(joke_request_agent, "get_joke_bank", lambda: fake_bank)

    ctrl = _ctrl()
    agent = joke_request_agent.JokeRequestAgent(ctrl)
    await agent.bid(_ctx("說個笑話")).handler()  # 不該 raise
    # bank 空 → 還是說了句 fallback（寧可有回應）
    ctrl.speak.assert_awaited_once()


async def test_handler_swallows_speak_exception(monkeypatch):
    from intent_agents import joke_request_agent

    fake_bank = MagicMock()
    fake_bank.random_joke.return_value = "笑話"
    monkeypatch.setattr(joke_request_agent, "get_joke_bank", lambda: fake_bank)

    ctrl = _ctrl()
    ctrl.speak = AsyncMock(side_effect=RuntimeError("boom"))
    agent = joke_request_agent.JokeRequestAgent(ctrl)
    await agent.bid(_ctx("說個笑話")).handler()  # 不該 raise
