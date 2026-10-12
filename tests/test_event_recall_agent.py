"""EventRecallAgent — 「我上個月去哪裡露營」問答（事件記憶 Phase 1）。"""
from __future__ import annotations

import json
import re
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from event_memory import EventStore, NOT_REMEMBER
from intent_agents.event_recall_agent import EventRecallAgent
from intent_bus import IntentContext



def _ctx(speaker: str, query: str, mode: str = "normal") -> IntentContext:
    return IntentContext(
        speaker=speaker,
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


def _ctrl(online_members, store):
    ctrl = MagicMock()
    ctrl.get_online_members = MagicMock(return_value=online_members)
    ctrl.play_tts = AsyncMock()
    ctrl.bot.router._call_llm = AsyncMock(return_value=json.dumps({"ids": []}))
    return ctrl


def test_manifest_description_non_empty():
    agent = EventRecallAgent(_ctrl([], EventStore(":memory:")))
    schema = agent.declare_intents()[0]
    assert schema.manifest_description


@pytest.mark.parametrize("query", ["我上個月去哪裡露營", "showay 什麼時候要出國"])
def test_regex_matches_positive_samples(query):
    agent = EventRecallAgent(_ctrl([], EventStore(":memory:")))
    schema = agent.declare_intents()[0]
    assert any(re.search(p, query) for p in schema.patterns), query


@pytest.mark.parametrize("query", ["上次那首歌再放一次", "去哪裡買"])
def test_regex_no_match_negative_samples(query):
    agent = EventRecallAgent(_ctrl([], EventStore(":memory:")))
    schema = agent.declare_intents()[0]
    assert not any(re.search(p, query) for p in schema.patterns), query


@pytest.mark.asyncio
async def test_handler_present_in_heard_by_speaks_event(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "1")
    store = EventStore(":memory:")
    now = time.time()
    store.add(1, {"owner": "showay", "activity": "露營", "place": "溪頭", "event_date": now - 86400,
                  "date_text": "", "participants": [], "quote": "下禮拜六我要去溪頭露營"},
              ["showay", "狗與露"], now)
    eid = store.list_guild(1, 0)[0]["id"]

    ctrl = _ctrl(["狗與露"], store)
    ctrl.bot.router._call_llm = AsyncMock(return_value=json.dumps({"ids": [eid]}))
    agent = EventRecallAgent(ctrl, store=store)

    bid = agent.bid(_ctx("showay", "showay 上次去哪裡露營"))
    await bid.handler()

    spoken = ctrl.play_tts.call_args.args[0]
    assert "露營" in spoken
    assert "下禮拜六我要去溪頭露營" in spoken


@pytest.mark.asyncio
async def test_handler_present_not_in_heard_by_not_remember_no_llm_call(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "1")
    store = EventStore(":memory:")
    now = time.time()
    store.add(1, {"owner": "showay", "activity": "露營", "place": "溪頭", "event_date": now - 86400,
                  "date_text": "", "participants": [], "quote": ""},
              ["showay"], now)  # 狗與露 不在 heard_by

    ctrl = _ctrl(["狗與露"], store)
    agent = EventRecallAgent(ctrl, store=store)

    bid = agent.bid(_ctx("showay", "showay 上次去哪裡露營"))
    await bid.handler()

    spoken = ctrl.play_tts.call_args.args[0]
    assert spoken == NOT_REMEMBER
    ctrl.bot.router._call_llm.assert_not_awaited()


@pytest.mark.parametrize("content", ['{"ids":[999]}', "not json"])
@pytest.mark.asyncio
async def test_handler_llm_bad_response_not_remember(monkeypatch, content):
    monkeypatch.setenv("GUILD_ID", "1")
    store = EventStore(":memory:")
    now = time.time()
    store.add(1, {"owner": "showay", "activity": "露營", "place": "溪頭", "event_date": now - 86400,
                  "date_text": "", "participants": [], "quote": ""},
              ["showay"], now)

    ctrl = _ctrl(["showay"], store)
    ctrl.bot.router._call_llm = AsyncMock(return_value=content)
    agent = EventRecallAgent(ctrl, store=store)

    bid = agent.bid(_ctx("showay", "我上次去哪裡露營"))
    await bid.handler()
    assert ctrl.play_tts.call_args.args[0] == NOT_REMEMBER


@pytest.mark.asyncio
async def test_handler_llm_raises_not_remember(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "1")
    store = EventStore(":memory:")
    now = time.time()
    store.add(1, {"owner": "showay", "activity": "露營", "place": "溪頭", "event_date": now - 86400,
                  "date_text": "", "participants": [], "quote": ""},
              ["showay"], now)

    ctrl = _ctrl(["showay"], store)
    ctrl.bot.router._call_llm = AsyncMock(side_effect=RuntimeError("boom"))
    agent = EventRecallAgent(ctrl, store=store)

    bid = agent.bid(_ctx("showay", "我上次去哪裡露營"))
    await bid.handler()
    assert ctrl.play_tts.call_args.args[0] == NOT_REMEMBER


@pytest.mark.asyncio
async def test_handler_event_without_date_not_remember_no_llm_call(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "1")
    store = EventStore(":memory:")
    now = time.time()
    store.add(1, {"owner": "showay", "activity": "露營", "place": "溪頭", "event_date": None,
                  "date_text": "", "participants": [], "quote": "下禮拜六我要去溪頭露營"},
              ["showay", "狗與露"], now)

    ctrl = _ctrl(["狗與露"], store)
    agent = EventRecallAgent(ctrl, store=store)

    bid = agent.bid(_ctx("showay", "showay 上次去哪裡露營"))
    await bid.handler()

    assert ctrl.play_tts.call_args.args[0] == NOT_REMEMBER
    ctrl.bot.router._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_event_older_than_30_days_not_spoken(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "1")
    store = EventStore(":memory:")
    now = time.time()
    store.add(1, {"owner": "showay", "activity": "露營", "place": "溪頭", "event_date": now - 86400,
                  "date_text": "", "participants": [], "quote": ""},
              ["showay"], now - 31 * 86400)

    ctrl = _ctrl(["showay"], store)
    agent = EventRecallAgent(ctrl, store=store)

    bid = agent.bid(_ctx("showay", "我上次去哪裡露營"))
    await bid.handler()
    assert ctrl.play_tts.call_args.args[0] == NOT_REMEMBER
    ctrl.bot.router._call_llm.assert_not_awaited()
