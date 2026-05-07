"""
Phase 2 WakeSignalFusion — test suite
Run: python test_phase2_fusion.py
"""
import asyncio
import json
import os
import sys
import time
import types
import tempfile
import logging
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")
sys.path.insert(0, os.path.dirname(__file__))

# Redirect stats file to a temp path so tests don't touch wake_stats.json
import wake_signal_fusion as _wsf_mod
_tmp_dir = tempfile.mkdtemp()
_wsf_mod._STATS_FILE = os.path.join(_tmp_dir, "wake_stats_test.json")

from wake_signal_fusion import WakeSignalFusion
from stt_cleaner import GeminiRouterSTTMixin, WAKE_THRESHOLD

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []

def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label))
    print(f"  {status}  {label}" + (f" — {detail}" if detail else ""))
    return condition


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1: WakeSignalFusion unit tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_fusion():
    print("\n" + "═" * 60)
    print("PART 1: WakeSignalFusion unit tests")
    print("═" * 60)

    f = WakeSignalFusion()

    # ── Baseline threshold ────────────────────────────────────────────────────
    t = f.get_threshold("Alice", context_active=False)
    check("Baseline threshold = 0.70", t == 0.70, f"t={t}")

    # ── Context penalty raises threshold ─────────────────────────────────────
    t = f.get_threshold("Alice", context_active=True)
    check("Context penalty adds 0.10", t == 0.80, f"t={t}")

    # ── Cold-start: < 5 interactions → no speaker penalty ────────────────────
    for _ in range(4):
        f.record_outcome("NoisySpeaker", False)
    t = f.get_threshold("NoisySpeaker", context_active=False)
    check("< 5 interactions → no speaker penalty yet", t == 0.70, f"t={t}")

    # ── Speaker penalty kicks in after 5 interactions, false_rate > 0.4 ──────
    f.record_outcome("NoisySpeaker", False)  # 5th — all false wakes (rate=1.0)
    t = f.get_threshold("NoisySpeaker", context_active=False)
    check("≥5 interactions, false_rate=1.0 → speaker penalty (+0.10)", t == 0.80, f"t={t}")

    # ── Both penalties stack ──────────────────────────────────────────────────
    t = f.get_threshold("NoisySpeaker", context_active=True)
    check("Both penalties stack → 0.90", t == 0.90, f"t={t}")

    # ── Threshold clamped at 0.95 ─────────────────────────────────────────────
    f2 = WakeSignalFusion()
    f2.BASE_THRESHOLD = 0.90
    f2.CONTEXT_PENALTY = 0.10
    f2.SPEAKER_PENALTY = 0.10
    for _ in range(5):
        f2.record_outcome("Loud", False)
    t = f2.get_threshold("Loud", context_active=True)
    check("Threshold clamped at 0.95", t == 0.95, f"t={t}")

    # ── Threshold clamped at 0.50 minimum ────────────────────────────────────
    f3 = WakeSignalFusion()
    f3.BASE_THRESHOLD = 0.40
    t = f3.get_threshold("Bob", context_active=False)
    check("Threshold clamped at 0.50 minimum", t == 0.50, f"t={t}")

    # ── decide() returns correct bool ────────────────────────────────────────
    f4 = WakeSignalFusion()
    wake, th = f4.decide(0.80, "Bob", False)
    check("decide(0.80) → wake=True at baseline", wake is True and th == 0.70, f"wake={wake} th={th}")

    wake, th = f4.decide(0.69, "Bob", False)
    check("decide(0.69) → wake=False at baseline", wake is False, f"wake={wake}")

    wake, th = f4.decide(0.75, "Bob", context_active=True)
    check("decide(0.75) with context_active → wake=False (threshold=0.80)", wake is False, f"wake={wake} th={th}")

    # ── True wakes do NOT raise threshold ────────────────────────────────────
    f5 = WakeSignalFusion()
    for _ in range(10):
        f5.record_outcome("GoodSpeaker", True)
    t = f5.get_threshold("GoodSpeaker", context_active=False)
    check("Good speaker (all true wakes) → no penalty", t == 0.70, f"t={t}")

    # ── Mixed speaker: false_rate exactly 0.4 → no penalty ───────────────────
    f6 = WakeSignalFusion()
    for _ in range(3):
        f6.record_outcome("Mixed", False)
    for _ in range(7):
        f6.record_outcome("Mixed", True)  # false_rate = 0.3 / 1.0 = 0.3
    # Actually 3 false + 7 true = 10 total, false_rate = 0.3
    t = f6.get_threshold("Mixed", context_active=False)
    check("false_rate=0.30 (≤0.4) → no speaker penalty", t == 0.70, f"t={t}")

    # ── Exactly 0.4 false rate → no penalty (boundary condition) ─────────────
    f7 = WakeSignalFusion()
    for _ in range(2):
        f7.record_outcome("Border", False)
    for _ in range(3):
        f7.record_outcome("Border", True)  # 2 false + 3 true = 5 total, rate=0.4
    t = f7.get_threshold("Border", context_active=False)
    check("false_rate=0.40 (not > 0.4) → no penalty", t == 0.70, f"t={t}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2: Persistence tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_persistence():
    print("\n" + "═" * 60)
    print("PART 2: Persistence tests")
    print("═" * 60)

    stats_path = _wsf_mod._STATS_FILE

    # Write some data
    f = WakeSignalFusion()
    for _ in range(3):
        f.record_outcome("Jack", False)
    for _ in range(7):
        f.record_outcome("Jack", True)

    check("wake_stats.json written to disk", os.path.exists(stats_path))

    # Reload from disk
    f2 = WakeSignalFusion()
    s = f2.speaker_stats.get("Jack", {})
    check("false_wakes persisted (3)", s.get("false_wakes") == 3, f"false_wakes={s.get('false_wakes')}")
    check("true_wakes persisted (7)", s.get("true_wakes") == 7, f"true_wakes={s.get('true_wakes')}")

    # Corrupt the file — should degrade gracefully
    with open(stats_path, "w") as fh:
        fh.write("{bad json!!!}")
    f3 = WakeSignalFusion()
    check("Corrupt stats file → graceful fallback (empty stats)", f3.speaker_stats == {})
    check("Corrupt stats file → baseline threshold still works", f3.get_threshold("Jack", False) == 0.70)


# ═══════════════════════════════════════════════════════════════════════════════
# PART 3: stt_cleaner integration — Phase 2 routing
# ═══════════════════════════════════════════════════════════════════════════════

def make_mock_instance(llm_response: str, fusion: WakeSignalFusion = None):
    class MockChoice:
        class message:
            content = llm_response

    class MockResponse:
        choices = [MockChoice()]
        usage = types.SimpleNamespace(total_tokens=50)

    class MockGroqClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    return MockResponse()

    class MockPromptManager:
        def get_instruction(self, layer, **kwargs):
            import marvin_prompts
            pm = marvin_prompts.PromptManager.__new__(marvin_prompts.PromptManager)
            pm.__init__()
            return pm.get_instruction(layer, **kwargs)

    inst = GeminiRouterSTTMixin.__new__(GeminiRouterSTTMixin)
    inst.groq_dedicated_client = MockGroqClient()
    inst.groq_cleaner_usage = []
    inst.groq_simple_model = None
    inst.prompt_manager = MockPromptManager()
    inst.wake_fusion = fusion or WakeSignalFusion()
    return inst


async def test_stt_integration():
    print("\n" + "═" * 60)
    print("PART 3: stt_cleaner ↔ WakeSignalFusion integration")
    print("═" * 60)

    # ── context_active=True raises threshold → 0.72 intent no longer wakes ───
    fusion = WakeSignalFusion()
    inst = make_mock_instance('{"cleaned": "馬文你好嗎", "intent": 0.72, "calling": true}', fusion)
    res = await inst.clean_stt_text("馬文你好嗎", speaker="Jack", context_active=True)
    check(
        "intent=0.72 + context_active → no wake (threshold raised to 0.80)",
        res["is_wake"] is False,
        f"is_wake={res['is_wake']} threshold={res['wake_threshold']}"
    )
    check("wake_threshold reported as 0.80", res["wake_threshold"] == 0.80, f"th={res['wake_threshold']}")

    # ── context_active=False → same intent wakes ──────────────────────────────
    inst2 = make_mock_instance('{"cleaned": "馬文你好嗎", "intent": 0.72, "calling": true}', WakeSignalFusion())
    res2 = await inst2.clean_stt_text("馬文你好嗎", speaker="Jack", context_active=False)
    check(
        "intent=0.72 + context_active=False → wake (threshold=0.70)",
        res2["is_wake"] is True,
        f"is_wake={res2['is_wake']} threshold={res2['wake_threshold']}"
    )

    # ── Noisy speaker gets raised threshold ───────────────────────────────────
    fusion2 = WakeSignalFusion()
    for _ in range(5):
        fusion2.record_outcome("NoisyGuy", False)  # false_rate=1.0 → +0.10
    inst3 = make_mock_instance('{"cleaned": "馬文幫我", "intent": 0.75, "calling": true}', fusion2)
    res3 = await inst3.clean_stt_text("馬文幫我", speaker="NoisyGuy", context_active=False)
    check(
        "Noisy speaker (false_rate=1.0): intent=0.75 no longer wakes (threshold=0.80)",
        res3["is_wake"] is False,
        f"is_wake={res3['is_wake']} threshold={res3['wake_threshold']}"
    )

    # ── No fusion on instance → Phase 1 static threshold used ────────────────
    inst4 = GeminiRouterSTTMixin.__new__(GeminiRouterSTTMixin)

    class MockGroqClient2:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    class R:
                        choices = [types.SimpleNamespace(message=types.SimpleNamespace(content='{"cleaned":"馬文你好","intent":0.72,"calling":true}'))]
                        usage = types.SimpleNamespace(total_tokens=10)
                    return R()

    class MP2:
        def get_instruction(self, *a, **kw):
            return "dummy"

    inst4.groq_dedicated_client = MockGroqClient2()
    inst4.groq_cleaner_usage = []
    inst4.groq_simple_model = None
    inst4.prompt_manager = MP2()
    # No wake_fusion attribute
    res4 = await inst4.clean_stt_text("馬文你好", speaker="Jack", context_active=True)
    check(
        "No wake_fusion → Phase 1 static threshold (intent=0.72 → wake since 0.72 >= 0.70)",
        res4["is_wake"] is True,
        f"is_wake={res4['is_wake']} threshold={res4['wake_threshold']}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
async def main():
    test_fusion()
    test_persistence()
    await test_stt_integration()

    print("\n" + "═" * 60)
    passed = sum(1 for s, _ in results if s == PASS)
    failed = sum(1 for s, _ in results if s == FAIL)
    print(f"TOTAL: {passed} passed, {failed} failed")
    print("═" * 60)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
