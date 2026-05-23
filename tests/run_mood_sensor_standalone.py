"""
tests/run_mood_sensor_standalone.py — Standalone test for M2 MoodSensor

繞 pytest（本機 environment broken）。
Run: venv_simon/bin/python tests/run_mood_sensor_standalone.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mood_sensor as ms


PASSED = 0
FAILED = 0
FAILURES = []


def run(name, fn):
    global PASSED, FAILED
    try:
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1
    except Exception as e:
        print(f"  ✗ {name} ERROR: {type(e).__name__}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_mock_transcript(text="hi", speaker="A", ts=None):
    return {"speaker": speaker, "text": text, "timestamp": ts or time.time()}


def _mk_groq_returning(label_text: str):
    """Make a mock AsyncGroq client whose chat.completions.create() returns given text."""
    mock = MagicMock()
    msg = MagicMock()
    msg.message = MagicMock()
    msg.message.content = label_text
    resp = MagicMock()
    resp.choices = [msg]
    mock.chat = MagicMock()
    mock.chat.completions = MagicMock()
    mock.chat.completions.create = AsyncMock(return_value=resp)
    return mock


def _mk_failing_groq():
    mock = MagicMock()
    mock.chat = MagicMock()
    mock.chat.completions = MagicMock()
    mock.chat.completions.create = AsyncMock(side_effect=RuntimeError("LLM down"))
    return mock


def _mk_transcript_store(transcripts):
    store = MagicMock()
    store.get_recent = MagicMock(return_value=list(transcripts))
    return store


def _mk_temp_monitor(temperature=0.7):
    tm = MagicMock()
    tm.temperature = temperature
    return tm


# ── parse_mood_label ─────────────────────────────────────────────────────────

def t_parse_mood_clean():
    for label in ms.MOOD_LABELS:
        assert ms.parse_mood_label(label) == label, f"clean parse: {label}"

def t_parse_mood_with_extra():
    assert ms.parse_mood_label("結果是: 興奮") == "興奮"
    assert ms.parse_mood_label("放鬆。") == "放鬆"

def t_parse_mood_no_match():
    assert ms.parse_mood_label("無關內容") is None
    assert ms.parse_mood_label("") is None
    assert ms.parse_mood_label(None) is None


# ── MoodSensor cache ─────────────────────────────────────────────────────────

async def t_cache_within_ttl():
    store = _mk_transcript_store([_mk_mock_transcript() for _ in range(3)])
    groq = _mk_groq_returning("興奮")
    sensor = ms.MoodSensor(store, groq, _mk_temp_monitor())

    v1 = await sensor.current_vibe(guild_id=1)
    v2 = await sensor.current_vibe(guild_id=1)
    assert v1.mood == "興奮"
    assert v1 is v2, "cache should return same object"
    # LLM should only be called once
    assert groq.chat.completions.create.await_count == 1, \
        f"expect 1 call got {groq.chat.completions.create.await_count}"


async def t_force_refresh_bypasses_cache():
    store = _mk_transcript_store([_mk_mock_transcript() for _ in range(3)])
    groq = _mk_groq_returning("興奮")
    sensor = ms.MoodSensor(store, groq, _mk_temp_monitor())

    await sensor.current_vibe(guild_id=1)
    await sensor.current_vibe(guild_id=1, force_refresh=True)
    assert groq.chat.completions.create.await_count == 2


async def t_invalidate_forces_recompute():
    store = _mk_transcript_store([_mk_mock_transcript() for _ in range(3)])
    groq = _mk_groq_returning("興奮")
    sensor = ms.MoodSensor(store, groq, _mk_temp_monitor())

    await sensor.current_vibe(guild_id=1)
    sensor.invalidate()
    await sensor.current_vibe(guild_id=1)
    assert groq.chat.completions.create.await_count == 2


# ── Fallback rules ───────────────────────────────────────────────────────────

async def t_no_conversation_returns_default_no_convo():
    """< MIN_TRANSCRIPTS_FOR_LLM → default 不打 LLM。"""
    store = _mk_transcript_store([])  # 完全沒對話
    groq = _mk_groq_returning("興奮")
    sensor = ms.MoodSensor(store, groq, _mk_temp_monitor(temperature=0.3))

    v = await sensor.current_vibe(guild_id=1)
    assert v.mood == ms.DEFAULT_MOOD, f"expect {ms.DEFAULT_MOOD} got {v.mood}"
    assert v.source == "default_no_convo"
    assert v.engagement == 0.3, "engagement 應該用 temperature_monitor 真值"
    assert groq.chat.completions.create.await_count == 0, "不該打 LLM"


async def t_llm_fail_uses_stale_cache_when_available():
    """第一次成功 cache，第二次 LLM 失敗 → fallback stale_cache。"""
    store = _mk_transcript_store([_mk_mock_transcript() for _ in range(3)])
    # 首次 LLM ok
    groq = _mk_groq_returning("興奮")
    sensor = ms.MoodSensor(store, groq, _mk_temp_monitor(temperature=0.8))

    v1 = await sensor.current_vibe(guild_id=1)
    assert v1.mood == "興奮"
    assert v1.source == "llm"

    # 模擬 LLM 失敗
    groq.chat.completions.create = AsyncMock(side_effect=RuntimeError("down"))
    sensor.invalidate()
    v2 = await sensor.current_vibe(guild_id=1)
    assert v2.mood == "興奮", "should keep stale mood"
    assert v2.source == "stale_cache"
    assert v2.engagement == 0.8


async def t_llm_fail_3_times_returns_default():
    """連續 3 LLM fail → 不再用 stale cache、回 default。"""
    store = _mk_transcript_store([_mk_mock_transcript() for _ in range(3)])
    groq_ok = _mk_groq_returning("興奮")
    sensor = ms.MoodSensor(store, groq_ok, _mk_temp_monitor(temperature=0.5))

    # 先 cache 一次
    await sensor.current_vibe(guild_id=1)
    assert sensor._cache.mood == "興奮"

    # 切到 failing groq
    sensor._groq = _mk_failing_groq()

    # 3 連 fail
    for i in range(3):
        sensor.invalidate()
        v = await sensor.current_vibe(guild_id=1)
    # 第 3 次（_consecutive_fails == 3）→ 回 default_fallback
    assert v.source == "default_fallback", f"expect default_fallback got {v.source}"
    assert v.mood == ms.DEFAULT_MOOD


async def t_engagement_always_from_temperature_monitor():
    """即使 LLM fail，engagement 仍取 temperature 真值。"""
    store = _mk_transcript_store([_mk_mock_transcript() for _ in range(3)])
    sensor = ms.MoodSensor(store, _mk_failing_groq(), _mk_temp_monitor(temperature=0.92))
    v = await sensor.current_vibe(guild_id=1)
    assert v.engagement == 0.92, f"expect engagement=0.92 got {v.engagement}"


async def t_engagement_falls_back_on_temp_exception():
    store = _mk_transcript_store([_mk_mock_transcript() for _ in range(3)])
    bad_temp = MagicMock()
    # accessing .temperature raises
    type(bad_temp).temperature = property(lambda self: (_ for _ in ()).throw(RuntimeError("temp broken")))
    sensor = ms.MoodSensor(store, _mk_groq_returning("放鬆"), bad_temp)
    v = await sensor.current_vibe(guild_id=1)
    assert v.engagement == 0.5, "temp 失敗 → 0.5 fallback"


# ── concurrency ──────────────────────────────────────────────────────────────

async def t_concurrent_current_vibe_only_one_llm_call():
    """N 個並發 current_vibe() 應該只觸發一次 LLM call（lock 保護）。"""
    store = _mk_transcript_store([_mk_mock_transcript() for _ in range(3)])
    # 加 small sleep 模擬 LLM 慢
    async def _slow_create(*args, **kwargs):
        await asyncio.sleep(0.05)
        msg = MagicMock()
        msg.message = MagicMock()
        msg.message.content = "興奮"
        resp = MagicMock()
        resp.choices = [msg]
        return resp
    groq = MagicMock()
    groq.chat = MagicMock()
    groq.chat.completions = MagicMock()
    groq.chat.completions.create = AsyncMock(side_effect=_slow_create)

    sensor = ms.MoodSensor(store, groq, _mk_temp_monitor())
    results = await asyncio.gather(*[sensor.current_vibe(guild_id=1) for _ in range(5)])
    assert all(r.mood == "興奮" for r in results)
    assert groq.chat.completions.create.await_count == 1, \
        f"concurrent 5 calls should LLM once got {groq.chat.completions.create.await_count}"


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== M2 mood_sensor.py standalone tests ===\n")

    print("parse_mood_label:")
    run("clean labels", t_parse_mood_clean)
    run("with extra text", t_parse_mood_with_extra)
    run("no match → None", t_parse_mood_no_match)
    print()

    print("MoodSensor cache:")
    run("within TTL same object", t_cache_within_ttl)
    run("force_refresh bypasses", t_force_refresh_bypasses_cache)
    run("invalidate forces recompute", t_invalidate_forces_recompute)
    print()

    print("Fallback rules:")
    run("no conversation → default_no_convo", t_no_conversation_returns_default_no_convo)
    run("LLM fail (1st) → stale_cache", t_llm_fail_uses_stale_cache_when_available)
    run("LLM fail 3 times → default", t_llm_fail_3_times_returns_default)
    run("engagement from temperature even on fail", t_engagement_always_from_temperature_monitor)
    run("engagement fallback on temp exception", t_engagement_falls_back_on_temp_exception)
    print()

    print("Concurrency:")
    run("N concurrent calls → 1 LLM", t_concurrent_current_vibe_only_one_llm_call)

    print()
    print(f"=== Results: {PASSED} passed, {FAILED} failed ===")
    if FAILED:
        print("\n--- Failures ---")
        for name, tb in FAILURES:
            print(f"\n{name}:")
            print(tb)
        sys.exit(1)
