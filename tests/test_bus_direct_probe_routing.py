"""TDD: IntentBus direct_probe — yt-dlp 直查優先於 curation resolver。

Why: 「播放七里香」（純歌名）目前命中 weak_play_artist_only → bus 路由到 resolver →
LLM 把「七里香」當歌手 → 幻覺或失敗。對照「播放周杰倫」LLM 認得是歌手能正常 curation。
方向：song_choice 缺槽時，先試 yt-dlp 直查；命中就跳過 curation 直接播 handler；
miss 才走原 resolver 路徑。

設計刻意：
- direct_probe 只在 slot == "song_choice" 觸發；directional_resolution（抽象修飾，
  user 明確要 LLM 解析）不該被 probe 短路。
- probe 例外不殺 dispatch — log warning 後 fall through 到 resolver（graceful degrade）。
- 沒設 direct_probe → 現有 resolver flow 完整保留（後向相容）。
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.semantic_resolver import ResolvedIntent, SpeakerProfile
from intent_bus import Bid, IntentBus, IntentContext


# ── Helpers ──────────────────────────────────────────────────────────────────

class _StubAgent:
    def __init__(self, name, bid_fn):
        self.name = name
        self._bid_fn = bid_fn

    def bid(self, ctx):
        return self._bid_fn(ctx)


def _ctx(query, depth=0, speaker="大肚"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, depth=depth,
    )


def _fake_resolver(resolved: ResolvedIntent | None, handled_slots=("song_choice", "directional_resolution")):
    r = MagicMock()
    r.handles = MagicMock(side_effect=lambda s: s in handled_slots)
    r.resolve = AsyncMock(return_value=resolved)
    return r


# ── 1. probe 命中 → resolver 不跑，winner.handler() 直接播 ──────────────────

@pytest.mark.asyncio
async def test_direct_probe_hit_skips_resolver_and_calls_handler():
    play_handler = AsyncMock()
    resolver = _fake_resolver(ResolvedIntent(rewritten_query="不該被用到", depth=1))
    probe = AsyncMock(return_value=True)

    bus = IntentBus(
        [_StubAgent("music", lambda c: Bid("music", 0.85, play_handler, "curation",
                                            missing_slots=["song_choice"]))],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        direct_probe=probe,
    )

    winner = await bus.dispatch(_ctx("播放七里香"))

    probe.assert_awaited_once_with("播放七里香")
    play_handler.assert_awaited_once()
    resolver.resolve.assert_not_awaited()
    assert winner is not None and winner.name == "music"


# ── 2. probe miss → 走原 resolver 路徑 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_direct_probe_miss_falls_through_to_resolver():
    curation_handler = AsyncMock()
    specific_handler = AsyncMock()
    probe = AsyncMock(return_value=None)  # falsy = 沒命中

    def _bid(ctx):
        if ctx.depth == 0:
            return Bid("music", 0.85, curation_handler, "curation",
                       missing_slots=["song_choice"])
        return Bid("music", 0.95, specific_handler, "specific", missing_slots=[])

    resolver = _fake_resolver(ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1))
    bus = IntentBus(
        [_StubAgent("music", _bid)],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        direct_probe=probe,
    )

    await bus.dispatch(_ctx("播放亂打字"))

    probe.assert_awaited_once_with("播放亂打字")
    resolver.resolve.assert_awaited_once()
    specific_handler.assert_awaited_once()
    curation_handler.assert_not_awaited()


# ── 3. directional_resolution slot 不被 probe 短路 ──────────────────────────

@pytest.mark.asyncio
async def test_direct_probe_skipped_for_directional_resolution_slot():
    """user 明確帶抽象修飾（符合年紀/適合心情）→ 必須走 LLM 解析，不能 yt-dlp 短路。"""
    curation_handler = AsyncMock()
    specific_handler = AsyncMock()
    probe = AsyncMock(return_value=True)  # 即使 probe 會命中

    def _bid(ctx):
        if ctx.depth == 0:
            return Bid("music", 0.50, curation_handler, "directional",
                       missing_slots=["directional_resolution"])
        return Bid("music", 0.95, specific_handler, "specific", missing_slots=[])

    resolver = _fake_resolver(ResolvedIntent(rewritten_query="播放周杰倫的七里香", depth=1))
    bus = IntentBus(
        [_StubAgent("music", _bid)],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        direct_probe=probe,
    )

    await bus.dispatch(_ctx("播放周杰倫符合我年紀的歌"))

    probe.assert_not_awaited()
    resolver.resolve.assert_awaited_once()
    specific_handler.assert_awaited_once()


# ── 4. 沒設 direct_probe → 現有 resolver flow 完整保留 ──────────────────────

@pytest.mark.asyncio
async def test_no_direct_probe_keeps_resolver_flow():
    curation_handler = AsyncMock()
    specific_handler = AsyncMock()

    def _bid(ctx):
        if ctx.depth == 0:
            return Bid("music", 0.85, curation_handler, "curation",
                       missing_slots=["song_choice"])
        return Bid("music", 0.95, specific_handler, "specific", missing_slots=[])

    resolver = _fake_resolver(ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1))
    bus = IntentBus(
        [_StubAgent("music", _bid)],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        # 不傳 direct_probe
    )

    await bus.dispatch(_ctx("播放周杰倫"))

    resolver.resolve.assert_awaited_once()
    specific_handler.assert_awaited_once()


# ── 5. probe 例外不殺 dispatch，fall through 到 resolver ────────────────────

@pytest.mark.asyncio
async def test_direct_probe_exception_falls_back_to_resolver(caplog):
    curation_handler = AsyncMock()
    specific_handler = AsyncMock()
    probe = AsyncMock(side_effect=RuntimeError("yt-dlp 連線炸"))

    def _bid(ctx):
        if ctx.depth == 0:
            return Bid("music", 0.85, curation_handler, "curation",
                       missing_slots=["song_choice"])
        return Bid("music", 0.95, specific_handler, "specific", missing_slots=[])

    resolver = _fake_resolver(ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1))
    bus = IntentBus(
        [_StubAgent("music", _bid)],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        direct_probe=probe,
    )

    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller.intent_bus"):
        await bus.dispatch(_ctx("播放周杰倫"))

    probe.assert_awaited_once()
    resolver.resolve.assert_awaited_once()
    specific_handler.assert_awaited_once()
    # 至少要有一條 warning log 提到 probe 炸了
    assert any("direct_probe" in r.message.lower() or "probe" in r.message.lower()
               for r in caplog.records if r.levelno >= logging.WARNING)


# ── 6. probe 命中時，recommendation_sink 不該被叫（沒 resolve 過）─────────

@pytest.mark.asyncio
async def test_direct_probe_hit_does_not_trigger_recommendation_sink():
    """probe 短路掉 curation → 沒有 ResolvedIntent，sink 不該被叫。"""
    sink = MagicMock()
    play_handler = AsyncMock()
    resolver = _fake_resolver(ResolvedIntent(rewritten_query="x", depth=1, selected="y"))
    probe = AsyncMock(return_value=True)

    bus = IntentBus(
        [_StubAgent("music", lambda c: Bid("music", 0.85, play_handler, "curation",
                                            missing_slots=["song_choice"]))],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        recommendation_sink=sink, direct_probe=probe,
    )

    await bus.dispatch(_ctx("播放七里香"))

    sink.assert_not_called()
    play_handler.assert_awaited_once()
