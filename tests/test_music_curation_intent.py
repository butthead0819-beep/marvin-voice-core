"""TDD: SemanticResolver for vector intent CURATION + DIRECTIONAL slots.

Resolver design (memory/project_vector_intent_5_21.md):
- CURATION  — bid 0.85, missing_slots=["song_choice"]
- DIRECTIONAL — bid 0.50, missing_slots=["directional_resolution"]
- Resolver looks at speaker_profile + raw_query → rewrites to a SPECIFIC command query
- Re-dispatch with depth+1; depth >= MAX_DEPTH → return None (caller falls through to Marvin)

2026-05-21: resolver now calls TieredLLMRouter.analyze (Tier 2 pool, 多家 70b 自動分流)
instead of a direct Cerebras client. These tests mock router.analyze → JSON content string.
Pool-level failover/cooldown is covered by tests/test_llm_pool.py.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.semantic_resolver import (
    MAX_REWRITE_DEPTH,
    ResolvedIntent,
    SemanticResolver,
    SpeakerProfile,
)


# ── Fixtures ───────────────────────────────────────────────────────────────

def _router_returning(song: str, quip: str = "", year: int | None = None):
    """Fake TieredLLMRouter whose analyze() returns the resolved JSON content string."""
    payload = {"song": song, "quip": quip}
    if year is not None:
        payload["year"] = year
    r = MagicMock()
    r.analyze = AsyncMock(return_value=json.dumps(payload, ensure_ascii=False))
    return r


def _router_returning_content(content: str):
    """Fake router returning an arbitrary content string (invalid-JSON case)."""
    r = MagicMock()
    r.analyze = AsyncMock(return_value=content)
    return r


def _router_none():
    """Fake router whose analyze() returns None (pool exhausted / all endpoints failed)."""
    r = MagicMock()
    r.analyze = AsyncMock(return_value=None)
    return r


def _profile_35yo_deep_night():
    return SpeakerProfile(
        speaker="大肚",
        birth_year=1990,
        age=35,
        recent_played=["林俊傑 江南", "五月天 倔強"],
        time_of_day="late_night",
        current_mood="reflective",
        who_else_in_channel=[],
    )


# ── 1. CURATION (artist-only) ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_curation_fills_song_choice_for_artist_only():
    """「播周杰倫」+ missing=[song_choice] → resolver rewrites to specific query."""
    router = _router_returning(song="夜曲", year=2005, quip="深夜聽周杰倫的人都有故事")
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="song_choice",
        raw_query="周杰倫",
        profile=_profile_35yo_deep_night(),
        depth=0,
    )

    assert result is not None
    assert isinstance(result, ResolvedIntent)
    assert "周杰倫" in result.rewritten_query
    assert "夜曲" in result.rewritten_query
    assert result.depth == 1, "depth should increment on resolve"


@pytest.mark.asyncio
async def test_curation_prompt_includes_speaker_profile():
    """Resolver 必須把 age + recent_played + time_of_day 都注入 prompt（analyze 的 user msg）。"""
    router = _router_returning(song="七里香", year=2004)
    resolver = SemanticResolver(router=router)

    await resolver.resolve(
        missing_slot="song_choice",
        raw_query="周杰倫",
        profile=_profile_35yo_deep_night(),
        depth=0,
    )

    # router.analyze(user_msg, caller=..., system=..., ...) — user_msg 是第一個位置參數
    user_msg = router.analyze.call_args.args[0]
    assert "35" in user_msg or "1990" in user_msg, "age signal missing"
    assert "林俊傑 江南" in user_msg, "recent_played not injected"
    assert "late_night" in user_msg or "深夜" in user_msg, "time_of_day not injected"
    assert "周杰倫" in user_msg, "raw_query not injected"
    # caller 必填（per-agent 歸屬）
    assert router.analyze.call_args.kwargs.get("caller") == "semantic_resolver"


# ── 2. DIRECTIONAL (abstract modifier) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_directional_resolves_to_concrete_query():
    """「周杰倫符合我年紀的歌」+ missing=[directional_resolution] → 具體 query."""
    router = _router_returning(song="七里香", year=2004, quip="懷舊就懷舊大方點")
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="directional_resolution",
        raw_query="周杰倫符合我年紀的歌",
        profile=_profile_35yo_deep_night(),
        depth=0,
    )

    assert result is not None
    assert "周杰倫" in result.rewritten_query
    assert "七里香" in result.rewritten_query or "2004" in result.rewritten_query


# ── 3. Depth limit (anti-infinite-loop) ────────────────────────────────────

@pytest.mark.asyncio
async def test_depth_at_max_returns_none_no_router_call():
    """depth >= MAX 不該打 router，直接 None 讓 caller 兜底。"""
    router = _router_returning(song="x")
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="song_choice",
        raw_query="周杰倫",
        profile=_profile_35yo_deep_night(),
        depth=MAX_REWRITE_DEPTH,
    )

    assert result is None
    assert router.analyze.await_count == 0


# ── 4. Failure modes (graceful degradation) ────────────────────────────────

@pytest.mark.asyncio
async def test_router_exhausted_returns_none():
    """池全冷卻/失敗 → router.analyze 回 None → resolver 回 None，caller 走 Marvin 兜底。"""
    router = _router_none()
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="song_choice",
        raw_query="周杰倫",
        profile=_profile_35yo_deep_night(),
        depth=0,
    )

    assert result is None


@pytest.mark.asyncio
async def test_no_router_returns_none():
    """沒 router（boot 時池沒接的場景）→ None。"""
    resolver = SemanticResolver(router=None)

    result = await resolver.resolve(
        missing_slot="song_choice",
        raw_query="周杰倫",
        profile=_profile_35yo_deep_night(),
        depth=0,
    )

    assert result is None


@pytest.mark.asyncio
async def test_invalid_json_returns_none():
    """router 回非 JSON → 安全降級 None。"""
    router = _router_returning_content("this is not json at all")
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="song_choice",
        raw_query="周杰倫",
        profile=_profile_35yo_deep_night(),
        depth=0,
    )

    assert result is None


# ── 5. Quip passthrough (Marvin persona) ───────────────────────────────────

@pytest.mark.asyncio
async def test_quip_attached_to_resolved_intent():
    """router 回 quip → 串到 ResolvedIntent.quip（handler 之後可做 TTS 串場詞）。"""
    router = _router_returning(song="稻香", quip="嘆氣 又是一個想找回青春的人類")
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="song_choice",
        raw_query="周杰倫",
        profile=_profile_35yo_deep_night(),
        depth=0,
    )

    assert result is not None
    assert result.quip == "嘆氣 又是一個想找回青春的人類"


# ── 6. Unknown slot name ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_missing_slot_returns_none():
    """未知 slot name（非 song_choice / directional_resolution）→ None，不打 router。"""
    router = _router_returning(song="x")
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="some_unknown_slot",
        raw_query="周杰倫",
        profile=_profile_35yo_deep_night(),
        depth=0,
    )

    assert result is None
    assert router.analyze.await_count == 0


# ── 7. selected 曲名暴露（給 recommendation log）────────────────────────────

@pytest.mark.asyncio
async def test_resolved_intent_exposes_selected_song():
    """ResolvedIntent.selected = resolver 選的乾淨曲名（"夜曲"），非整句 rewritten_query。"""
    router = _router_returning(song="夜曲", quip="x")
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="song_choice", raw_query="周杰倫",
        profile=_profile_35yo_deep_night(), depth=0,
    )

    assert result is not None
    assert result.selected == "夜曲"


@pytest.mark.asyncio
async def test_genre_tail_stripped_so_artist_is_clean():
    """「播放陶喆的歌曲」curation → _compose_query 剝掉「的歌曲」→ 乾淨指令句，不留類別詞。"""
    router = _router_returning(song="找自己")
    resolver = SemanticResolver(router=router)

    result = await resolver.resolve(
        missing_slot="song_choice", raw_query="播放陶喆的歌曲",
        profile=_profile_35yo_deep_night(), depth=0,
    )

    assert result is not None
    assert result.rewritten_query == "播放陶喆的找自己"
    assert "歌曲" not in result.rewritten_query   # 類別詞不該殘留污染 yt-dlp
