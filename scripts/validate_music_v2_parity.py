"""4-Gate validator for MusicAgent v2 declarative architecture.

跑 317 個歷史 wake events 過 v1 和 v2，量四個 gate：

  Gate 1 — Behavior parity: v2 vs v1 same (confidence_bucket, missing_slots)
  Gate 2 — Negative space: v2 dense bid rate（v1=None 的 case 是否 v2 dense 0.0）
  Gate 3 — Slot extraction: weak_play_long_string 是否能正確萃出 target
  Gate 4 — Code economy: lines of code v1 vs v2 (informational, 不卡 pass/fail)

任一 Gate 1/2 fail → 架構不適合，停手寫 post-mortem。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from intent_agents.music_agent import MusicAgent  # noqa: E402
from intent_agents.music_agent_v2 import MusicAgentV2  # noqa: E402
from intent_bus import IntentContext  # noqa: E402
from scripts.replay_bid_history import (  # noqa: E402
    OWNER_NAMES,
    _FakeController,
    parse_log_files,
)


def _bucket(conf):
    """Round confidence to known buckets to ignore minor differences."""
    if conf is None:
        return None
    for b in (0.95, 0.80, 0.55, 0.30, 0.0):
        if abs(conf - b) < 0.01:
            return b
    return round(conf, 2)


def _summarize(bid):
    if bid is None:
        return None
    return (_bucket(bid.confidence), tuple(bid.missing_slots))


def run_validation(log_paths: list[str]):
    print(f"📂 讀 {len(log_paths)} 個 log...")
    events, _outcomes = parse_log_files(log_paths)
    print(f"📊 抽到 {len(events)} 條 wake events\n")

    ctrl = _FakeController()
    v1 = MusicAgent(ctrl)
    v2 = MusicAgentV2(ctrl)

    # Buckets
    parity_match = 0
    parity_mismatch = 0
    mismatches = []
    v2_dense_zero_count = 0
    v1_none_v2_zero = 0
    v1_none_v2_nonzero = 0  # 不該發生
    v1_bid_v2_none = 0  # 不該發生
    slot_extraction_cases = []  # weak_play_long_string 的 target slot

    for ev in events:
        ctx = IntentContext(
            speaker=ev.speaker, raw_text=ev.raw_text, query=ev.query,
            original_raw=ev.raw_text, wake_intent=ev.wake_intent,
            stream_active=False, game_mode=False,
            is_owner=ev.speaker in OWNER_NAMES, now=0.0,
        )
        b1 = v1.bid(ctx)
        b2 = v2.bid(ctx)

        # v2 should never return None — always dense bid
        if b2 is None:
            print(f"  ⚠️ v2 returned None on '{ev.query[:40]}'")
            continue

        s1 = _summarize(b1)
        s2 = _summarize(b2)

        # Negative-space tracking
        if b1 is None:
            if b2.confidence == 0.0:
                v1_none_v2_zero += 1
            else:
                v1_none_v2_nonzero += 1
                mismatches.append((ev.query, s1, s2, b2.reason))
        else:
            if b2.confidence == 0.0:
                v1_bid_v2_none += 1
                mismatches.append((ev.query, s1, s2, b2.reason))

        # Behavior parity: when both bid (non-zero), compare bucket + missing_slots
        if b1 is not None and b2.confidence > 0.0:
            if s1 == s2:
                parity_match += 1
            else:
                parity_mismatch += 1
                mismatches.append((ev.query, s1, s2, b2.reason))

        if b2.confidence == 0.0:
            v2_dense_zero_count += 1

        # Slot extraction: weak_play_long_string should have target slot
        if b2.reason.startswith("weak_play_only:"):
            slot_extraction_cases.append((ev.query, b2.reason))

    n = len([e for e in events if e.query])
    print("══════════ Gate Results ══════════\n")

    # Gate 1 — Behavior parity
    total_compare = parity_match + parity_mismatch + v1_none_v2_nonzero + v1_bid_v2_none
    parity_rate = parity_match / total_compare if total_compare else 0.0
    print(f"Gate 1 — Behavior parity")
    print(f"  v1 與 v2 都 bid 且 (confidence, missing_slots) 相同: {parity_match}")
    print(f"  v1 與 v2 都 bid 但 mismatch:                       {parity_mismatch}")
    print(f"  v1=None 但 v2 非 0.0（不該發生）:                  {v1_none_v2_nonzero}")
    print(f"  v1 bid 但 v2=0.0（不該發生）:                      {v1_bid_v2_none}")
    print(f"  → parity rate: {parity_rate:.1%}")
    gate1_pass = parity_rate >= 0.98 and v1_none_v2_nonzero == 0 and v1_bid_v2_none == 0
    print(f"  {'✅ PASS' if gate1_pass else '❌ FAIL'} (threshold ≥98% + no impossible transitions)\n")

    # Gate 2 — Negative space density
    v1_none_count = sum(1 for ev in events if v1.bid(IntentContext(
        speaker=ev.speaker, raw_text=ev.raw_text, query=ev.query,
        original_raw=ev.raw_text, wake_intent=ev.wake_intent,
        stream_active=False, game_mode=False,
        is_owner=ev.speaker in OWNER_NAMES, now=0.0)) is None)
    dense_rate = v2_dense_zero_count / v1_none_count if v1_none_count else 0.0
    print(f"Gate 2 — Negative space")
    print(f"  v1 returned None: {v1_none_count}")
    print(f"  v2 returned dense 0.0: {v2_dense_zero_count}")
    print(f"  → dense rate (v2 zero / v1 None): {dense_rate:.1%}")
    gate2_pass = dense_rate >= 0.95
    print(f"  {'✅ PASS' if gate2_pass else '❌ FAIL'} (threshold ≥95%)\n")

    # Gate 3 — Slot extraction
    print(f"Gate 3 — Slot extraction (weak_play_long_string targets)")
    print(f"  cases captured: {len(slot_extraction_cases)}")
    valid_targets = 0
    bad_targets = []
    for q, reason in slot_extraction_cases:
        # reason 格式: weak_play_only:{kw}->{target}
        target_part = reason.split("->", 1)
        if len(target_part) == 2 and target_part[1].strip():
            valid_targets += 1
        else:
            bad_targets.append((q, reason))
    slot_rate = valid_targets / len(slot_extraction_cases) if slot_extraction_cases else 1.0
    print(f"  valid (non-empty) targets: {valid_targets}/{len(slot_extraction_cases)} ({slot_rate:.1%})")
    if slot_extraction_cases[:3]:
        print(f"  sample:")
        for q, r in slot_extraction_cases[:3]:
            print(f"    `{q[:30]}` → {r}")
    gate3_pass = slot_rate >= 0.95
    print(f"  {'✅ PASS' if gate3_pass else '❌ FAIL'} (threshold ≥95%)\n")

    # Gate 4 — Code economy
    v1_lines = (REPO_ROOT / "intent_agents/music_agent.py").read_text().count("\n")
    v2_lines = (REPO_ROOT / "intent_agents/music_agent_v2.py").read_text().count("\n")
    base_lines = (REPO_ROOT / "intent_agents/base.py").read_text().count("\n")
    print(f"Gate 4 — Code economy (informational)")
    print(f"  v1 music_agent.py:    {v1_lines} lines")
    print(f"  v2 music_agent_v2.py: {v2_lines} lines")
    print(f"  base.py (one-time):   {base_lines} lines (amortized across all agents)")
    economy_ok = v2_lines <= v1_lines * 1.3
    print(f"  v2 alone: {'≤' if economy_ok else '>'} v1 +30% threshold "
          f"({'✅' if economy_ok else '⚠️'})\n")

    # Mismatch detail
    if mismatches:
        print(f"\n══════ Mismatch detail (first 15) ══════")
        for q, s1, s2, reason in mismatches[:15]:
            print(f"  q=`{q[:40]}` v1={s1} v2={s2} reason='{reason}'")

    print("\n══════════ Verdict ══════════")
    all_pass = gate1_pass and gate2_pass and gate3_pass
    print(f"Gate 1 (parity):       {'✅' if gate1_pass else '❌'}")
    print(f"Gate 2 (negative):     {'✅' if gate2_pass else '❌'}")
    print(f"Gate 3 (slot):         {'✅' if gate3_pass else '❌'}")
    print(f"Gate 4 (code):         {'✅' if economy_ok else '⚠️ informational'}")
    print(f"\n→ {'✅ ARCHITECTURE VALIDATED, proceed to migrate other agents' if all_pass else '❌ ARCHITECTURE NEEDS REWORK'}")
    return all_pass


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    run_validation(sys.argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
