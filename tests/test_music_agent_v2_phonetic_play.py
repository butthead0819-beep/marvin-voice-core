"""TDD：MusicAgentV2 播放動詞音素 fallback（phonetic_play schema）。

STT 把「播放」糊成同音字（泡放/抱放…）時，6 個既有 schema 全靠字面 kw regex，
會整批 miss。喚醒+動詞拼音都中才出價（AND-gate，見 intent_agents/base.py 模組
docstring），confidence 0.55（比照 weak_play_long_string 最低檔），缺 song_title
→ 走 _ask_music_followup（bus 不認得 song_title 這個 slot 名，不會自動路由
resolver，見 intent_bus.py 293-295 行）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from intent_agents.music_agent_v2 import MusicAgentV2
from intent_bus import IntentContext


class _FakeCtrl:
    async def _safe_music_command(self, *a, **kw):
        pass

    async def _ask_music_followup(self, *a, **kw):
        pass


def _ctx(query, wake_intent=0.9):
    return IntentContext(
        speaker="x", raw_text=query, query=query, original_raw=query,
        wake_intent=wake_intent, stream_active=False, game_mode=False,
        is_owner=False, now=0.0,
    )


@pytest.fixture
def agent():
    return MusicAgentV2(_FakeCtrl())


def test_garbled_play_verb_with_target_bids_phonetic(agent):
    # 「播放」→「泡放」，regex 全 miss，拼音 fallback 接住
    bid = agent.bid(_ctx("泡放周杰倫的稻香"))
    assert bid.confidence == 0.55
    assert bid.reason.startswith("phonetic:")
    assert bid.missing_slots == ["song_title"]


def test_garbled_play_verb_low_wake_intent_no_bid(agent):
    # LOW_WAKE_THRESHOLD=0.65 在 gate() 就擋掉，AND-gate 的另一半沒中
    bid = agent.bid(_ctx("泡放周杰倫的稻香", wake_intent=0.3))
    assert bid.confidence == 0.0


def test_exact_play_verb_still_wins_over_phonetic(agent):
    # 字面命中 strong_play，不該掉進 phonetic fallback
    bid = agent.bid(_ctx("放一首陶喆"))
    assert bid.confidence == 0.95
    assert not bid.reason.startswith("phonetic:")


def test_unrelated_query_does_not_trigger_phonetic_play(agent):
    bid = agent.bid(_ctx("今天天氣真好"))
    assert bid.confidence == 0.0


@pytest.mark.asyncio
async def test_garbled_play_verb_handler_asks_followup_not_direct_play():
    ctrl = _FakeCtrl()
    ctrl._ask_music_followup = AsyncMock()
    ctrl._safe_music_command = AsyncMock()
    agent = MusicAgentV2(ctrl)
    bid = agent.bid(_ctx("泡放周杰倫的稻香"))
    await bid.handler()
    ctrl._ask_music_followup.assert_awaited_once()
    ctrl._safe_music_command.assert_not_awaited()
