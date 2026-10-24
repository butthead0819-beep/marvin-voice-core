"""對談窗內指令也要喚醒詞（2026-10-24 使用者定）。

窗內 target 沒叫名字的句子「有可能只是在討論，並沒有要執行 action」——
IntentBus 只放行唯讀 agent（查詢/問歌名/時間/笑話/回憶/遊戲知識/guard），
其他 winner 一律不執行，回 WAKE_REQUIRED 哨兵；WakeSession 記一次沒回應、安靜不回話。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import WAKE_REQUIRED, Bid, IntentBus, IntentContext
from wake_session import WakeSession


def _ctx(wakeless_in_window: bool, query="下一首"):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=True, game_mode=False,
        is_owner=False, now=0.0, mode="stream", dispatch_source="test",
        in_wake_session=True, wakeless_in_window=wakeless_in_window,
    )


class _Agent:
    def __init__(self, name):
        self.name = name
        self.handler = AsyncMock()

    def bid(self, ctx):
        return Bid(name=self.name, confidence=0.9, handler=self.handler, reason="hit")


def _bus(agent):
    return IntentBus([agent])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["music", "playback_control", "volume", "replay", "queue_control",
                                  "song_feedback", "current_album", "personal_shuffle", "find_song",
                                  "taste_correction", "nemoclaw", "frustration"])
async def test_action_winner_blocked_without_wake_in_window(name):
    agent = _Agent(name)
    winner = await _bus(agent).dispatch(_ctx(True))
    agent.handler.assert_not_called()
    assert winner.name == WAKE_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["grounded_qa", "now_playing", "time_query", "joke_request",
                                  "event_recall", "game_knowledge", "guard"])
async def test_readonly_winner_allowed_without_wake_in_window(name):
    agent = _Agent(name)
    winner = await _bus(agent).dispatch(_ctx(True, query="那他有那麼厲害嗎"))
    agent.handler.assert_awaited_once()
    assert winner.name == name


@pytest.mark.asyncio
async def test_action_winner_runs_when_wake_word_present():
    agent = _Agent("playback_control")
    winner = await _bus(agent).dispatch(_ctx(False, query="馬文下一首"))
    agent.handler.assert_awaited_once()
    assert winner.name == "playback_control"


# ── WakeSessionMixin：哨兵 → 記一次沒回應，不播提示音 ────────────────────────

from cogs.voice_controller_wake_session import WakeSessionMixin


class _FakeCog(WakeSessionMixin):
    _WAKE_PATTERNS = ["馬文", "老馬", "marvin"]

    def __init__(self):
        self._wake_session = WakeSession()
        self._mixer = None
        self._wake_response_pending = False
        self._wake_session_play_ack = AsyncMock()


def _listening(cog, speaker="Alice"):
    t0 = time.time()
    cog._wake_session.on_wake_accepted(speaker, t0)
    cog._wake_session.on_replied(t0)


def test_on_replied_wake_required_counts_unanswered_without_ack():
    cog = _FakeCog()
    _listening(cog)
    before = cog._wake_session.unanswered
    cog._wake_session_on_replied(Bid(name=WAKE_REQUIRED, confidence=0.0, handler=AsyncMock()))
    assert cog._wake_session.unanswered == before + 1
    assert cog._wake_session.target == "Alice"
    cog._wake_session_play_ack.assert_not_called()


@pytest.mark.parametrize("text,expected", [
    ("下一首", True),
    ("音樂大聲一點", True),
    ("馬文下一首", False),
    ("好啦老馬下一首", False),
    ("Marvin skip", False),
])
def test_wakeless_in_window_for_target(text, expected):
    cog = _FakeCog()
    _listening(cog)
    assert cog._wakeless_in_window("Alice", text) is expected


def test_wakeless_in_window_false_for_other_speaker_or_closed_window():
    cog = _FakeCog()
    _listening(cog)
    assert cog._wakeless_in_window("Bob", "下一首") is False
    cog2 = _FakeCog()
    assert cog2._wakeless_in_window("Alice", "下一首") is False


# ── voice_controller 接線：main bus ctx 帶 wakeless_in_window ────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("harvest,expected", [("再放一次", True), ("馬文再放一次", False)])
async def test_process_queued_query_marks_wakeless_in_window(harvest, expected):
    from tests.test_process_queued_query_wav_capture import _make_cog as _make_pq_cog
    cog = _make_pq_cog()
    s = WakeSession()
    t0 = time.time()
    s.on_wake_accepted("Alice", t0)
    s.on_replied(t0)
    cog._wake_session = s
    cog._wake_session_play_ack = AsyncMock()
    seen = {}

    async def _dispatch(ctx):
        seen["ctx"] = ctx
        return Bid(name="grounded_qa", confidence=0.9, handler=AsyncMock())
    cog._intent_bus.dispatch = _dispatch
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value=harvest)

    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95, original_raw=harvest)

    assert seen["ctx"].wakeless_in_window is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("harvest,expected_called", [("記一下，我等等要買菜", False), ("馬文記一下，我等等要買菜", True)])
async def test_pa_write_intent_needs_wake_in_window(harvest, expected_called):
    """記事/待辦是寫入動作：窗內沒叫名字比照低信心喚醒不執行。"""
    from tests.test_process_queued_query_wav_capture import _make_cog as _make_pq_cog
    cog = _make_pq_cog()
    s = WakeSession()
    t0 = time.time()
    s.on_wake_accepted("Alice", t0)
    s.on_replied(t0)
    cog._wake_session = s
    cog._wake_session_play_ack = AsyncMock()
    cog._recall_handler = MagicMock()
    cog._handle_manual_add_query = AsyncMock()
    cog._intent_bus.dispatch = AsyncMock(return_value=Bid(name=WAKE_REQUIRED, confidence=0.0, handler=AsyncMock()))
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value=harvest)

    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95, original_raw=harvest)

    assert cog._handle_manual_add_query.await_count == (1 if expected_called else 0)
