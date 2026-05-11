"""
Concurrent load test: 50 users online, 10 speaking simultaneously, 20 in text chat.

Tests:
  - No deadlocks or race conditions under concurrent load
  - STT serialization (stt_lock enforces ≤1 concurrent transcription)
  - MemoryManager (SQLite WAL) handles concurrent writes without data loss
  - SukiBudget token totals are accurate under concurrent increments
  - All 30 active requests complete within a reasonable wall-clock time
"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from suki_memory import MemoryManager
from suki_budget import SukiBudget
from marvin_voice_core.pipeline import MarvinVoicePipeline
from wake_detector import WakeDetector


# ── Constants ─────────────────────────────────────────────────────────────────

TOTAL_USERS    = 50
VOICE_SPEAKERS = 10   # simultaneous voice STT requests
TEXT_CHATTERS  = 20   # simultaneous text-chat LLM requests
TOKENS_PER_REQ = 500  # tokens charged per LLM call


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def mem(tmp_path):
    db    = str(tmp_path / "load_test.db")
    jpath = str(tmp_path / "load_mem.json")
    return MemoryManager(db_path=db, json_compat_path=jpath)


@pytest.fixture
def budget(tmp_path):
    return SukiBudget(db_path=str(tmp_path / "load_budget.db"), max_tokens=10_000_000)


@pytest.fixture
def pipeline(tmp_path):
    """MarvinVoicePipeline with mocked bot and STT subprocess."""
    bot = MagicMock()
    bot.guilds = []
    bot.loop   = asyncio.get_event_loop()
    bot.router = MagicMock()
    bot.router.game_dict_string = ""
    p = MarvinVoicePipeline(bot=bot, whisper_model=None)
    return p


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_users(n: int) -> list[str]:
    return [f"Player_{i:03d}" for i in range(n)]


async def simulate_voice_request(
    speaker: str,
    pipeline: MarvinVoicePipeline,
    mem: MemoryManager,
    budget: SukiBudget,
    stt_concurrency_counter: list,   # mutable list used as shared counter
    stt_max_observed: list,
) -> float:
    """Simulate a single voice user: STT → memory update → budget charge."""
    t0 = time.perf_counter()

    # STT with stt_lock (Semaphore(1)) — tracks concurrency
    async with pipeline.stt_lock:
        stt_concurrency_counter[0] += 1
        if stt_concurrency_counter[0] > stt_max_observed[0]:
            stt_max_observed[0] = stt_concurrency_counter[0]
        await asyncio.sleep(0.01)   # simulate ~10ms STT latency
        stt_concurrency_counter[0] -= 1

    # Memory: record interaction
    mem.increment_stat(speaker, "interaction_count")

    # Budget: charge tokens
    budget.add_tokens(TOKENS_PER_REQ)

    return time.perf_counter() - t0


async def simulate_text_request(
    speaker: str,
    mem: MemoryManager,
    budget: SukiBudget,
) -> float:
    """Simulate a text-chat user: memory read → mock LLM → budget charge."""
    t0 = time.perf_counter()

    # Fetch / create player memory
    mem.get_player_memory(speaker)

    # Simulate LLM latency (I/O-bound, yields to event loop)
    await asyncio.sleep(0.02)

    # Budget charge
    budget.add_tokens(TOKENS_PER_REQ)

    return time.perf_counter() - t0


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_50_users_online_memory_seeded(mem):
    """All 50 users can be written and read back without corruption."""
    users = make_users(TOTAL_USERS)

    async def seed(username):
        mem.get_player_memory(username)
        mem.increment_stat(username, "interaction_count")

    await asyncio.gather(*[seed(u) for u in users])

    for u in users:
        p = mem._cache[u]
        assert p["stats"]["interaction_count"] == 1.0, f"{u} stat missing"


@pytest.mark.asyncio
async def test_10_voice_stt_serialized(pipeline, mem, budget):
    """
    10 simultaneous STT requests must be serialized by stt_lock (Semaphore(1)).
    Maximum observed concurrency inside the lock must be exactly 1.
    """
    users = make_users(VOICE_SPEAKERS)
    counter      = [0]   # current concurrency inside stt_lock
    max_observed = [0]   # peak concurrency

    latencies = await asyncio.gather(*[
        simulate_voice_request(u, pipeline, mem, budget, counter, max_observed)
        for u in users
    ])

    # STT was never entered by more than 1 coroutine at once
    assert max_observed[0] == 1, (
        f"STT concurrency exceeded 1: observed {max_observed[0]}"
    )

    # All requests completed
    assert len(latencies) == VOICE_SPEAKERS

    # Budget accumulated correctly
    expected = VOICE_SPEAKERS * TOKENS_PER_REQ
    assert budget.tokens == expected, (
        f"Token mismatch: expected {expected}, got {budget.tokens}"
    )


@pytest.mark.asyncio
async def test_20_text_chat_concurrent(mem, budget):
    """
    20 simultaneous text-chat requests must all complete without errors,
    with correct budget totals.
    """
    # Use a separate user range so they don't collide with voice users
    users = [f"TextUser_{i:03d}" for i in range(TEXT_CHATTERS)]

    latencies = await asyncio.gather(*[
        simulate_text_request(u, mem, budget)
        for u in users
    ])

    assert len(latencies) == TEXT_CHATTERS

    expected = TEXT_CHATTERS * TOKENS_PER_REQ
    assert budget.tokens == expected

    # All users have memory records
    for u in users:
        assert u in mem._cache, f"{u} missing from cache"


@pytest.mark.asyncio
async def test_full_load_10_voice_20_text_concurrent(pipeline, mem, budget):
    """
    Full scenario: 10 voice + 20 text requests launched simultaneously.
    Asserts no deadlock, correct counts, and total wall time < 5s.
    """
    voice_users = make_users(VOICE_SPEAKERS)
    text_users  = [f"TextUser_{i:03d}" for i in range(TEXT_CHATTERS)]
    counter      = [0]
    max_observed = [0]

    t_start = time.perf_counter()

    results = await asyncio.gather(
        *[simulate_voice_request(u, pipeline, mem, budget, counter, max_observed)
          for u in voice_users],
        *[simulate_text_request(u, mem, budget)
          for u in text_users],
    )

    wall_time = time.perf_counter() - t_start

    # All 30 requests completed
    assert len(results) == VOICE_SPEAKERS + TEXT_CHATTERS

    # STT was serialized
    assert max_observed[0] == 1

    # Budget total: 30 requests × 500 tokens
    expected_tokens = (VOICE_SPEAKERS + TEXT_CHATTERS) * TOKENS_PER_REQ
    assert budget.tokens == expected_tokens, (
        f"Budget mismatch: expected {expected_tokens}, got {budget.tokens}"
    )

    # Voice speakers all have interaction_count == 1
    for u in voice_users:
        stat = mem.get_player_memory(u)["stats"]["interaction_count"]
        assert stat == 1.0, f"{u} interaction_count wrong: {stat}"

    # Text speakers all have records
    for u in text_users:
        assert u in mem._cache

    # Wall time sanity: serialized STT = 10 × 10ms = 100ms minimum,
    # text tasks overlap (20 × 20ms in parallel ≈ 20ms).
    # Total should be well under 5 seconds even on a slow CI machine.
    assert wall_time < 5.0, f"Load test too slow: {wall_time:.2f}s"

    print(
        f"\n✅ 10 voice + 20 text completed in {wall_time*1000:.0f}ms | "
        f"peak STT concurrency: {max_observed[0]} | "
        f"tokens charged: {budget.tokens}"
    )


@pytest.mark.asyncio
async def test_memory_no_lost_writes_under_concurrent_stat_increments(mem):
    """
    30 coroutines each increment the same user's interaction_count by 1.
    Final value must equal 30 (no lost writes due to SQLite WAL serialization).
    """
    N = 30
    username = "SharedPlayer"

    async def inc(_):
        mem.increment_stat(username, "interaction_count")

    await asyncio.gather(*[inc(i) for i in range(N)])

    final = mem.get_player_memory(username)["stats"]["interaction_count"]
    assert final == float(N), f"Lost writes: expected {N}, got {final}"


@pytest.mark.asyncio
async def test_budget_no_lost_tokens_under_concurrent_adds(budget):
    """
    50 coroutines each add 100 tokens.
    Final total must be exactly 5000.
    """
    N = 50
    PER = 100

    async def add(_):
        budget.add_tokens(PER)

    await asyncio.gather(*[add(i) for i in range(N)])

    assert budget.tokens == N * PER, (
        f"Lost tokens: expected {N * PER}, got {budget.tokens}"
    )


@pytest.mark.asyncio
async def test_wake_detector_concurrent_decisions():
    """
    WakeDetector.multi_channel_decide is stateless per call — safe to call
    concurrently from many coroutines without locking.
    """
    wd = WakeDetector()
    speakers = [f"Speaker_{i}" for i in range(VOICE_SPEAKERS)]

    async def decide(speaker):
        return wd.multi_channel_decide(
            action="fast_intervene",
            wake_intent=1.0,
            text="馬文幫我",
            speaker=speaker,
            context_active=False,
        )

    results = await asyncio.gather(*[decide(s) for s in speakers])

    assert len(results) == VOICE_SPEAKERS
    for should_wake, confidence, _ in results:
        assert should_wake is True
        assert confidence > 0.35
