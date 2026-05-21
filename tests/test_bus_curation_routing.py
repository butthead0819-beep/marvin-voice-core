"""TDD: IntentBus missing_slots → resolver → depth re-dispatch（vector intent Step 2）。

對應 memory/project_vector_intent_5_21.md「Step 2：IntentBus dispatch missing_slots routing」。
設計（A 案，2026-05-21 Jack 拍板）：
- winner 缺 resolver 認得的 slot（song_choice / directional_resolution）
  → resolver.resolve() 吐指令句 → 帶 depth+1 重投 bus → SPECIFIC 命中 → handler 播。
- resolver 不認得的 slot（song_title）→ 維持原本 winner.handler()（_ask 追問）。
- resolver 回 None（depth≥MAX / 失敗 / 無 client）→ llm_fallback 兜底。
- 沒設 resolver（現有 prod bus）→ missing_slots 忽略，直接 handler（後向相容）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.semantic_resolver import ResolvedIntent, SemanticResolver, SpeakerProfile
from intent_bus import Bid, IntentBus, IntentContext


# ── Helpers ──────────────────────────────────────────────────────────────────

class StubAgent:
    """bid() 由注入的 fn 決定，可依 ctx.depth 改變行為（模擬 curation→specific 重投）。"""
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


# ── 1. CURATION 重投：resolver 補完 → SPECIFIC 命中 → 原 curation handler 不跑 ───

@pytest.mark.asyncio
async def test_curation_redispatches_and_specific_handler_runs():
    curation_handler = AsyncMock()
    specific_handler = AsyncMock()

    def _bid(ctx):
        if ctx.depth == 0:
            return Bid("music", 0.85, curation_handler, "curation", missing_slots=["song_choice"])
        return Bid("music", 0.95, specific_handler, "specific", missing_slots=[])

    resolver = _fake_resolver(ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1))
    bus = IntentBus([StubAgent("music", _bid)], resolver=resolver,
                    profile_provider=lambda s: SpeakerProfile(speaker=s))

    await bus.dispatch(_ctx("播放周杰倫"))

    resolver.resolve.assert_awaited_once()
    # raw_query 是原 query，depth 從 ctx 帶入
    assert resolver.resolve.await_args.args[0] == "song_choice"
    assert resolver.resolve.await_args.args[1] == "播放周杰倫"
    specific_handler.assert_awaited_once()
    curation_handler.assert_not_awaited()


# ── 2. resolver 拿到 profile_provider 給的 profile ───────────────────────────

@pytest.mark.asyncio
async def test_resolver_gets_profile_from_provider():
    def _bid(ctx):
        if ctx.depth == 0:
            return Bid("music", 0.85, AsyncMock(), "curation", missing_slots=["song_choice"])
        return Bid("music", 0.95, AsyncMock(), "specific", missing_slots=[])

    resolver = _fake_resolver(ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1))
    bus = IntentBus([StubAgent("music", _bid)], resolver=resolver,
                    profile_provider=lambda s: SpeakerProfile(speaker=s, age=35))

    await bus.dispatch(_ctx("播放周杰倫", speaker="大肚"))

    profile = resolver.resolve.await_args.kwargs.get("profile") or resolver.resolve.await_args.args[2]
    assert profile.speaker == "大肚"
    assert profile.age == 35


# ── 3. depth 由 bus 傳給 resolver（resolver 自己的 MAX 守門才有效）─────────────

@pytest.mark.asyncio
async def test_bus_passes_ctx_depth_to_resolver():
    resolver = _fake_resolver(None)  # 模擬 resolver 在 depth 守門回 None
    llm_fallback = AsyncMock()
    bus = IntentBus(
        [StubAgent("music", lambda c: Bid("music", 0.85, AsyncMock(), "curation",
                                          missing_slots=["song_choice"]))],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        llm_fallback=llm_fallback,
    )

    await bus.dispatch(_ctx("播放周杰倫", depth=2))

    assert resolver.resolve.await_args.kwargs.get("depth") == 2 or \
           resolver.resolve.await_args.args[3] == 2


# ── 4. resolver 回 None → Marvin LLM 兜底 ────────────────────────────────────

@pytest.mark.asyncio
async def test_resolver_none_falls_back_to_llm():
    handler = AsyncMock()
    llm_fallback = AsyncMock()
    resolver = _fake_resolver(None)
    bus = IntentBus(
        [StubAgent("music", lambda c: Bid("music", 0.85, handler, "curation",
                                          missing_slots=["song_choice"]))],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        llm_fallback=llm_fallback,
    )

    await bus.dispatch(_ctx("播放周杰倫"))

    llm_fallback.assert_awaited_once()
    handler.assert_not_awaited()


# ── 5. song_title（resolver 不認得）→ 維持原 handler（_ask 追問）──────────────

@pytest.mark.asyncio
async def test_song_title_slot_not_routed_to_resolver():
    ask_handler = AsyncMock()
    llm_fallback = AsyncMock()
    resolver = _fake_resolver(ResolvedIntent(rewritten_query="x", depth=1))
    bus = IntentBus(
        [StubAgent("music", lambda c: Bid("music", 0.55, ask_handler, "longstring",
                                          missing_slots=["song_title"]))],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        llm_fallback=llm_fallback,
    )

    await bus.dispatch(_ctx("播放某個超長字串歌名"))

    ask_handler.assert_awaited_once()
    resolver.resolve.assert_not_awaited()
    llm_fallback.assert_not_awaited()


# ── 6. 沒設 resolver（現有 prod bus）→ missing_slots 忽略，直接 handler ────────

@pytest.mark.asyncio
async def test_no_resolver_configured_calls_handler():
    handler = AsyncMock()
    bus = IntentBus([StubAgent("music", lambda c: Bid("music", 0.85, handler, "curation",
                                                      missing_slots=["song_choice"]))])

    await bus.dispatch(_ctx("播放周杰倫"))

    handler.assert_awaited_once()


# ── 7. self-contained winner（無 missing）→ 直接 handler，不碰 resolver ────────

@pytest.mark.asyncio
async def test_self_contained_winner_calls_handler():
    handler = AsyncMock()
    resolver = _fake_resolver(ResolvedIntent(rewritten_query="x", depth=1))
    bus = IntentBus([StubAgent("music", lambda c: Bid("music", 0.95, handler, "specific",
                                                      missing_slots=[]))],
                    resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s))

    await bus.dispatch(_ctx("播放陶喆的天天"))

    handler.assert_awaited_once()
    resolver.resolve.assert_not_awaited()


# ── 8. 端到端整合：真 MusicAgentV2 + 真 resolver（mock Cerebras）─────────────

@pytest.mark.asyncio
async def test_real_v2_curation_redispatch_hits_specific_play():
    """「播放周杰倫」→ CURATION → resolver 吐「播放周杰倫的夜曲」→ 重投 → SPECIFIC →
    真 handler 呼叫 ctrl._safe_music_command（含 rewritten query）。驗 A 案端到端。"""
    import json
    from types import SimpleNamespace
    from intent_agents.music_agent_v2 import MusicAgentV2

    ctrl = MagicMock()
    ctrl._safe_music_command = AsyncMock()

    # mock Cerebras：回 song=夜曲
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({"song": "夜曲", "quip": ""}, ensure_ascii=False)))])
    cerebras = MagicMock()
    cerebras.chat.completions.create = AsyncMock(return_value=response)
    resolver = SemanticResolver(cerebras_client=cerebras, model="llama-3.1-8b")

    bus = IntentBus([MusicAgentV2(ctrl)], resolver=resolver,
                    profile_provider=lambda s: SpeakerProfile(speaker=s, age=35))

    await bus.dispatch(_ctx("播放周杰倫"))

    ctrl._safe_music_command.assert_awaited_once()
    played_query = ctrl._safe_music_command.await_args.args[1]
    assert "周杰倫" in played_query and "夜曲" in played_query
    assert ctrl._safe_music_command.await_args.args[2] == "play"


# ── 9. recommendation_sink（Step 4：resolve 成功記推薦事件）──────────────────

@pytest.mark.asyncio
async def test_recommendation_sink_called_on_resolve():
    sink = MagicMock()

    def _bid(ctx):
        if ctx.depth == 0:
            return Bid("music", 0.85, AsyncMock(), "curation", missing_slots=["song_choice"])
        return Bid("music", 0.95, AsyncMock(), "specific", missing_slots=[])

    resolver = _fake_resolver(ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1, selected="夜曲"))
    bus = IntentBus([StubAgent("music", _bid)], resolver=resolver,
                    profile_provider=lambda s: SpeakerProfile(speaker=s),
                    recommendation_sink=sink)

    await bus.dispatch(_ctx("播放周杰倫"))

    sink.assert_called_once()
    slot_arg, _ctx_arg, resolved_arg = sink.call_args.args
    assert slot_arg == "song_choice"
    assert resolved_arg.selected == "夜曲"


@pytest.mark.asyncio
async def test_recommendation_sink_not_called_when_resolver_none():
    sink = MagicMock()
    resolver = _fake_resolver(None)
    bus = IntentBus(
        [StubAgent("music", lambda c: Bid("music", 0.85, AsyncMock(), "curation",
                                          missing_slots=["song_choice"]))],
        resolver=resolver, profile_provider=lambda s: SpeakerProfile(speaker=s),
        recommendation_sink=sink, llm_fallback=AsyncMock(),
    )

    await bus.dispatch(_ctx("播放周杰倫"))

    sink.assert_not_called()


@pytest.mark.asyncio
async def test_recommendation_sink_exception_does_not_break_dispatch():
    """sink 炸了不能斷 wake path — resolve 後仍重投命中 specific。"""
    sink = MagicMock(side_effect=RuntimeError("disk full"))
    specific_handler = AsyncMock()

    def _bid(ctx):
        if ctx.depth == 0:
            return Bid("music", 0.85, AsyncMock(), "curation", missing_slots=["song_choice"])
        return Bid("music", 0.95, specific_handler, "specific", missing_slots=[])

    resolver = _fake_resolver(ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1, selected="夜曲"))
    bus = IntentBus([StubAgent("music", _bid)], resolver=resolver,
                    profile_provider=lambda s: SpeakerProfile(speaker=s),
                    recommendation_sink=sink)

    await bus.dispatch(_ctx("播放周杰倫"))

    specific_handler.assert_awaited_once()
