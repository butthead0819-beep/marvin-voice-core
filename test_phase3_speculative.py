"""
Phase 3 Speculative Prefetch — test suite
Run: python test_phase3_speculative.py
"""
import asyncio
import sys
import os
import logging
from unittest.mock import MagicMock

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")
sys.path.insert(0, os.path.dirname(__file__))

from gemini_router_llm import GeminiRouterLLMMixin
from wake_signal_fusion import WakeSignalFusion
import wake_signal_fusion as _wsf_mod

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label))
    print(f"  {status}  {label}" + (f" — {detail}" if detail else ""))
    return condition


# ── Minimal router stub that lets us control stream_fast_response ─────────────

class StubRouter:
    """Minimal stub: inherits _speculative_response from the mixin only."""

    _speculative_response = GeminiRouterLLMMixin._speculative_response

    def __init__(self, chunks=None, raises=None):
        self._chunks = chunks or []
        self._raises = raises
        self._pending_prefetch: dict = {}
        self.wake_fusion = WakeSignalFusion()

    async def stream_fast_response(self, speaker, query, history=None, **kwargs):
        if self._raises:
            raise self._raises
        for c in self._chunks:
            yield c


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1: _speculative_response unit tests
# ═══════════════════════════════════════════════════════════════════════════════

async def test_speculative_response():
    print("\n" + "═" * 60)
    print("PART 1: _speculative_response unit tests")
    print("═" * 60)

    # ── Normal drain joins all chunks ─────────────────────────────────────────
    r = StubRouter(chunks=["你好", "，我是", "馬文。"])
    result = await r._speculative_response("Jack", "馬文你好")
    check("Normal drain → joined string", result == "你好，我是馬文。", f"result='{result}'")

    # ── __SEARCHING__ is filtered out ─────────────────────────────────────────
    r2 = StubRouter(chunks=["__SEARCHING__", "這是", "搜尋結果。"])
    result2 = await r2._speculative_response("Jack", "最新消息")
    check("__SEARCHING__ filtered out", result2 == "這是搜尋結果。", f"result='{result2}'")

    # ── Exception → returns empty string, does not raise ─────────────────────
    r3 = StubRouter(raises=RuntimeError("API down"))
    result3 = await r3._speculative_response("Jack", "anything")
    check("Exception → empty string, no raise", result3 == "", f"result='{result3}'")

    # ── Empty generator → returns empty string ────────────────────────────────
    r4 = StubRouter(chunks=[])
    result4 = await r4._speculative_response("Jack", "quiet")
    check("Empty generator → empty string", result4 == "", f"result='{result4}'")

    # ── Only __SEARCHING__ chunks → returns empty string ─────────────────────
    r5 = StubRouter(chunks=["__SEARCHING__", "__SEARCHING__"])
    result5 = await r5._speculative_response("Jack", "web query")
    check("Only __SEARCHING__ chunks → empty string", result5 == "", f"result='{result5}'")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2: _pending_prefetch lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

async def test_prefetch_lifecycle():
    print("\n" + "═" * 60)
    print("PART 2: _pending_prefetch lifecycle")
    print("═" * 60)

    # ── Task stored and retrievable ───────────────────────────────────────────
    r = StubRouter(chunks=["回應文字"])
    task = asyncio.create_task(r._speculative_response("Alice", "馬文幫我"))
    r._pending_prefetch["Alice"] = task
    await asyncio.sleep(0)  # let task run
    popped = r._pending_prefetch.pop("Alice", None)
    check("Task stored and popped by speaker key", popped is task)
    check("Dict empty after pop", "Alice" not in r._pending_prefetch)

    # ── Completed task has correct result ─────────────────────────────────────
    r2 = StubRouter(chunks=["你好嗎？"])
    task2 = asyncio.create_task(r2._speculative_response("Bob", "馬文"))
    await task2  # wait for completion
    check("Completed task result correct",
          task2.done() and task2.result() == "你好嗎？",
          f"result='{task2.result()}'")

    # ── Pending task can be cancelled ─────────────────────────────────────────
    async def _slow():
        await asyncio.sleep(60)
        return "never"

    slow_task = asyncio.create_task(_slow())
    slow_task.cancel()
    await asyncio.sleep(0)
    check("Pending task cancelled without error", slow_task.cancelled())

    # ── Cache-hit logic: done + non-empty → use prefetch ─────────────────────
    r3 = StubRouter(chunks=["預取回應"])
    t3 = asyncio.create_task(r3._speculative_response("Carol", "query"))
    await t3
    r3._pending_prefetch["Carol"] = t3

    _task = r3._pending_prefetch.pop("Carol", None)
    _prefetched = None
    if _task is not None and _task.done() and not _task.cancelled():
        try:
            _prefetched = _task.result() or None
        except Exception:
            pass
    check("Cache HIT: done task yields non-empty string", _prefetched == "預取回應",
          f"_prefetched='{_prefetched}'")

    # ── Cache-miss logic: empty result → None ────────────────────────────────
    r4 = StubRouter(chunks=[])  # empty → _speculative_response returns ""
    t4 = asyncio.create_task(r4._speculative_response("Dave", "query"))
    await t4
    r4._pending_prefetch["Dave"] = t4

    _task4 = r4._pending_prefetch.pop("Dave", None)
    _prefetched4 = None
    if _task4 is not None and _task4.done() and not _task4.cancelled():
        try:
            _prefetched4 = _task4.result() or None
        except Exception:
            pass
    check("Cache MISS: empty result → None (falls back to live call)", _prefetched4 is None)

    # ── Cache-miss logic: failed task → None ─────────────────────────────────
    r5 = StubRouter(raises=RuntimeError("boom"))
    t5 = asyncio.create_task(r5._speculative_response("Eve", "query"))
    await t5
    r5._pending_prefetch["Eve"] = t5

    _task5 = r5._pending_prefetch.pop("Eve", None)
    _prefetched5 = None
    if _task5 is not None and _task5.done() and not _task5.cancelled():
        try:
            _prefetched5 = _task5.result() or None
        except Exception:
            pass
    check("Cache MISS: exception-result task → None (falls back to live call)", _prefetched5 is None)


# ═══════════════════════════════════════════════════════════════════════════════
# PART 3: Engine prefetch trigger conditions
# ═══════════════════════════════════════════════════════════════════════════════

async def test_engine_trigger_conditions():
    """Simulate the conditional logic in discord_voice_engine._process_stt_hybrid."""
    print("\n" + "═" * 60)
    print("PART 3: Engine prefetch trigger conditions")
    print("═" * 60)

    def _should_prefetch(wake_intent, is_wake_b):
        """Replicates the engine's trigger condition."""
        _wi = wake_intent or 0.0
        return _wi >= 0.85 and is_wake_b

    check("intent=0.90, wake=True → prefetch fires",
          _should_prefetch(0.90, True))
    check("intent=0.85, wake=True → prefetch fires (boundary)",
          _should_prefetch(0.85, True))
    check("intent=0.84, wake=True → no prefetch (below threshold)",
          not _should_prefetch(0.84, True))
    check("intent=0.90, wake=False → no prefetch (fusion rejected)",
          not _should_prefetch(0.90, False))
    check("intent=None, wake=True → no prefetch (no intent score)",
          not _should_prefetch(None, True))
    check("intent=0.0, wake=False → no prefetch",
          not _should_prefetch(0.0, False))

    # ── High-confidence wake actually starts a task ───────────────────────────
    r = StubRouter(chunks=["預測回應"])
    r._pending_prefetch = {}

    wake_intent = 0.92
    is_wake_b = True
    speaker = "Frank"
    cleaned_text = "馬文你今天好嗎"

    if wake_intent >= 0.85 and is_wake_b:
        r._pending_prefetch[speaker] = asyncio.create_task(
            r._speculative_response(speaker, cleaned_text, [])
        )

    check("High-confidence wake creates task in _pending_prefetch",
          speaker in r._pending_prefetch and
          isinstance(r._pending_prefetch[speaker], asyncio.Task))

    await asyncio.gather(*r._pending_prefetch.values())  # drain tasks


# ═══════════════════════════════════════════════════════════════════════════════
# PART 4: record_outcome(speaker, True) wiring
# ═══════════════════════════════════════════════════════════════════════════════

async def test_true_wake_recording():
    print("\n" + "═" * 60)
    print("PART 4: record_outcome(speaker, True) wiring")
    print("═" * 60)

    # Patch stats file to a nonexistent temp path so WakeSignalFusion() here
    # starts with a clean slate, regardless of disk state from previous runs.
    _wsf_mod._STATS_FILE = "/tmp/wake_stats_test_part4_isolation.json"
    try:
        # Simulate the voice_controller logic:
        #   if full_text:
        #       ...
        #       _fusion = getattr(getattr(self.bot, 'router', None), 'wake_fusion', None)
        #       if _fusion:
        #           _fusion.record_outcome(speaker, True)

        fusion = WakeSignalFusion()

        def _simulate_response_complete(full_text: str, speaker: str):
            if full_text:
                fusion.record_outcome(speaker, True)

        _simulate_response_complete("你好，有什麼我能幫你的嗎？", "Grace")
        s = fusion.speaker_stats.get("Grace", {})
        check("True wake recorded after non-empty response",
              s.get("true_wakes") == 1 and s.get("false_wakes") == 0,
              f"stats={s}")

        _simulate_response_complete("", "Grace")  # empty → should NOT record
        s2 = fusion.speaker_stats.get("Grace", {})
        check("No record when response is empty",
              s2.get("true_wakes") == 1,  # still 1, not 2
              f"stats={s2}")

        # ── True wakes do not inflate the speaker penalty ─────────────────────
        for _ in range(10):
            fusion.record_outcome("GoodSpeaker", True)
        t = fusion.get_threshold("GoodSpeaker", context_active=False)
        check("10 true wakes → no speaker penalty (threshold stays 0.70)",
              t == 0.70, f"t={t}")

        # ── Mixed: false wakes + true wakes recorded from both paths ──────────
        fusion2 = WakeSignalFusion()
        # 3 false (from Phase 2 proxy), 7 true (from Phase 3 _process_queued_query)
        for _ in range(3):
            fusion2.record_outcome("Mixed", False)
        for _ in range(7):
            fusion2.record_outcome("Mixed", True)
        t2 = fusion2.get_threshold("Mixed", context_active=False)
        check("false_rate=0.30 after mixed feedback → no speaker penalty",
              t2 == 0.70, f"t={t2}")
    finally:
        if os.path.exists("/tmp/wake_stats_test_part4_isolation.json"):
            os.unlink("/tmp/wake_stats_test_part4_isolation.json")


# ═══════════════════════════════════════════════════════════════════════════════
async def main():
    await test_speculative_response()
    await test_prefetch_lifecycle()
    await test_engine_trigger_conditions()
    await test_true_wake_recording()

    print("\n" + "═" * 60)
    passed = sum(1 for s, _ in results if s == PASS)
    failed = sum(1 for s, _ in results if s == FAIL)
    print(f"TOTAL: {passed} passed, {failed} failed")
    print("═" * 60)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
