"""
Offline replay tool — 從歷史 log 抽 wake events，過一遍 IntentBus，
看現有的 confidence map 跟 legacy 路由的差異，找 calibration 邊界。

用法：
    python scripts/replay_bid_history.py records/daily/*.log

數據來源：
- [✅Query通過] [speaker] gate_ok | query='...' — 拿到 post-quality-gate 的 query
- [⚡喚醒] [speaker] raw='...' | wake_intent=... — 拿到 wake_intent
- [Music Command] / [NemoClaw→speaker] — 拿到 legacy 路由結果

不執行 handler，純收集 bids → 統計 + 對照 legacy outcome。
"""
from __future__ import annotations

import re
import sys
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

# 讓 script 從 repo root 跑能 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intent_bus import Bid, IntentContext
from intent_agents.music_agent import MusicAgent
from intent_agents.nemoclaw_agent import NemoClawAgent


# 從記憶得知 owner 是「狗與露」(Jack's Discord display name)
OWNER_NAMES = frozenset({"狗與露"})


@dataclass
class WakeEvent:
    ts: datetime
    speaker: str
    query: str
    raw_text: str
    wake_intent: float | None


@dataclass
class LegacyOutcome:
    kind: str   # "music_play" / "music_skip" / ... / "nemoclaw" / "marvin_or_skip"
    detail: str


@dataclass
class ReplayResult:
    event: WakeEvent
    bids: list[Bid]
    winner: Bid | None
    legacy: LegacyOutcome


# ── Log parsing ───────────────────────────────────────────────────────────────

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[.,]\d+")
_QUERY_OK_RE = re.compile(r"\[✅Query通過\] \[([^\]]+)\] gate_ok \| query='([^']*)'")
_WAKE_RE = re.compile(r"\[⚡喚醒\] \[([^\]]+)\] raw='([^']*)' \| Track=([AB]) \| wake_intent=(\S+)")
_MUSIC_CMD_RE = re.compile(r"\[Music Command\] (\S+) 觸發語音音樂指令: (\S+) \| query='([^']*)'")
_NEMOCLAW_OUT_RE = re.compile(r"\[NemoClaw→(\S+)\] Q='([^']*)'")


def _parse_ts(line: str) -> datetime | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_log_files(paths: Iterable[str]) -> tuple[list[WakeEvent], list[tuple[datetime, str]]]:
    """Returns (wake_events, outcome_markers).

    wake_events: [✅Query通過] 解析得到的 wake，speaker + query
    outcome_markers: 每條 (ts, marker_str) — 用來事後 join 找 legacy outcome
    """
    events: list[WakeEvent] = []
    wake_intents: dict[tuple[str, str], float | None] = {}  # (speaker, raw_text) → wake_intent
    outcomes: list[tuple[datetime, str]] = []

    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                ts = _parse_ts(line)
                if ts is None:
                    continue
                # 先收集 wake_intent (供後續對應)
                m = _WAKE_RE.search(line)
                if m:
                    speaker, raw_text, _track, intent_str = m.groups()
                    wi: float | None
                    if intent_str == "None":
                        wi = None
                    else:
                        try:
                            wi = float(intent_str)
                        except ValueError:
                            wi = None
                    wake_intents[(speaker, raw_text)] = wi
                    continue

                m = _QUERY_OK_RE.search(line)
                if m:
                    speaker, query = m.groups()
                    # 試著找對應的 wake_intent（用 speaker + 任意 raw_text 模糊匹配）
                    wi = None
                    raw = query
                    for (sp, rt), v in wake_intents.items():
                        if sp == speaker and (query in rt or rt in query):
                            wi = v
                            raw = rt
                            break
                    events.append(WakeEvent(ts=ts, speaker=speaker, query=query,
                                             raw_text=raw, wake_intent=wi))
                    continue

                m = _MUSIC_CMD_RE.search(line)
                if m:
                    speaker, cmd, q = m.groups()
                    outcomes.append((ts, f"music_{cmd}|{speaker}|{q}"))
                    continue
                m = _NEMOCLAW_OUT_RE.search(line)
                if m:
                    speaker, q = m.groups()
                    outcomes.append((ts, f"nemoclaw|{speaker}|{q}"))
                    continue

    outcomes.sort()
    return events, outcomes


def find_legacy_outcome(event: WakeEvent, outcomes: list[tuple[datetime, str]]) -> LegacyOutcome:
    """Look up legacy outcome within 30s after wake event for this speaker."""
    for ts, marker in outcomes:
        if ts < event.ts:
            continue
        delta = (ts - event.ts).total_seconds()
        if delta > 30:
            break
        parts = marker.split("|", 2)
        kind, speaker, _ = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
        if speaker != event.speaker:
            continue
        if kind.startswith("music_"):
            return LegacyOutcome(kind=kind, detail=parts[2] if len(parts) > 2 else "")
        if kind == "nemoclaw":
            return LegacyOutcome(kind="nemoclaw", detail=parts[2] if len(parts) > 2 else "")
    return LegacyOutcome(kind="marvin_or_skip", detail="")


# ── Replay setup ──────────────────────────────────────────────────────────────

class _FakeController:
    """Bid-time-only stub — 不執行 handler，只需要 keyword 常數。"""
    def __init__(self):
        from cogs.voice_controller import VoiceController as _VC
        self._STRONG_PLAY_KW   = _VC._STRONG_PLAY_KW
        self._WEAK_PLAY_KW     = _VC._WEAK_PLAY_KW
        self._MUSIC_SKIP_KW    = _VC._MUSIC_SKIP_KW
        self._MUSIC_STOP_KW    = _VC._MUSIC_STOP_KW
        self._MUSIC_PAUSE_KW   = _VC._MUSIC_PAUSE_KW
        self._MUSIC_RESUME_KW  = _VC._MUSIC_RESUME_KW

    async def _handle_voice_music_command(self, *a, **kw): pass
    async def _handle_nemoclaw_query(self, *a, **kw): pass


def replay_one(event: WakeEvent, agents: list) -> tuple[list[Bid], Bid | None]:
    ctx = IntentContext(
        speaker=event.speaker,
        raw_text=event.raw_text,
        query=event.query,
        original_raw=event.raw_text,
        wake_intent=event.wake_intent,
        stream_active=False,
        game_mode=False,
        is_owner=event.speaker in OWNER_NAMES,
        now=0.0,
    )
    bids: list[Bid] = []
    for agent in agents:
        try:
            b = agent.bid(ctx)
            if b is not None:
                bids.append(b)
        except Exception as e:
            print(f"⚠️  agent {agent.name} 炸了: {e}", file=sys.stderr)
    bids.sort(key=lambda b: b.confidence, reverse=True)
    winner = bids[0] if bids and bids[0].confidence >= 0.30 else None
    return bids, winner


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(results: list[ReplayResult]):
    n = len(results)
    print(f"════════════════════════════════════════════════════")
    print(f"分析 {n} 條歷史 wake events")
    print(f"════════════════════════════════════════════════════\n")

    # 1. Bus winner 分布
    winner_count = Counter()
    for r in results:
        winner_count[r.winner.name if r.winner else "no_bid_fall_to_legacy"] += 1
    print("【Bus winner 分布】")
    for k, v in winner_count.most_common():
        print(f"  {k:30s}  {v:5d}  ({v/n:.1%})")
    print()

    # 2. Legacy outcome 分布
    legacy_count = Counter(r.legacy.kind for r in results)
    print("【Legacy 實際路由】")
    for k, v in legacy_count.most_common():
        print(f"  {k:30s}  {v:5d}  ({v/n:.1%})")
    print()

    # 3. Bus ↔ Legacy 對照矩陣
    matrix = defaultdict(int)
    for r in results:
        bus = r.winner.name if r.winner else "no_bid"
        legacy_simple = "music" if r.legacy.kind.startswith("music_") else r.legacy.kind
        matrix[(bus, legacy_simple)] += 1
    print("【Bus ↔ Legacy 對照矩陣】(bus_winner × legacy_outcome)")
    print(f"  {'bus_winner':<20s} {'legacy':<20s} {'count':>6s}")
    for (bus, legacy), cnt in sorted(matrix.items(), key=lambda x: -x[1]):
        print(f"  {bus:<20s} {legacy:<20s} {cnt:6d}")
    print()

    # 4. 分歧 case（bus 跟 legacy 不一致）
    disagreements = []
    for r in results:
        bus = r.winner.name if r.winner else "no_bid"
        legacy_simple = "music" if r.legacy.kind.startswith("music_") else r.legacy.kind
        # bus=music vs legacy=marvin → false positive (bus 多接)
        # bus=no_bid vs legacy=music → false negative (bus 漏接)
        if bus == "music" and legacy_simple in ("marvin_or_skip", "nemoclaw"):
            disagreements.append(("FP_music", r))
        elif bus == "no_bid" and legacy_simple == "music":
            disagreements.append(("FN_music", r))
        elif bus == "nemoclaw" and legacy_simple != "nemoclaw":
            disagreements.append(("FP_nemoclaw", r))
    print(f"【分歧 cases】({len(disagreements)} 條 / {n} 總數)")
    by_type = defaultdict(list)
    for tag, r in disagreements:
        by_type[tag].append(r)
    for tag, rs in by_type.items():
        print(f"\n  ── {tag} ({len(rs)} 條) ──")
        # 顯示前 10 個 distinct query
        seen = set()
        shown = 0
        for r in rs:
            if r.event.query in seen: continue
            seen.add(r.event.query)
            winner = r.winner.name if r.winner else "—"
            conf = f"{r.winner.confidence:.2f}" if r.winner else "—"
            reason = r.winner.reason if r.winner else "—"
            print(f"    [{conf}] {winner:10s} legacy={r.legacy.kind:18s} "
                  f"q='{r.event.query[:50]}'  reason='{reason}'")
            shown += 1
            if shown >= 10: break

    print()

    # 5. 邊界 case (winner confidence < 0.65)
    borderline = [r for r in results if r.winner and r.winner.confidence < 0.65]
    print(f"\n【Borderline cases (winner.confidence < 0.65, 共 {len(borderline)} 條)】")
    seen = set()
    shown = 0
    for r in sorted(borderline, key=lambda r: r.winner.confidence):
        if r.event.query in seen: continue
        seen.add(r.event.query)
        print(f"  [{r.winner.confidence:.2f}] {r.winner.name:10s} "
              f"legacy={r.legacy.kind:18s} q='{r.event.query[:60]}' "
              f"reason='{r.winner.reason}'")
        shown += 1
        if shown >= 20: break

    print()

    # 6. Tie cases (top-2 confidence gap < 0.10)
    ties = []
    # 需要重新 replay 拿 full bid list
    for r in results:
        if len(r.bids) >= 2 and r.bids[0].confidence - r.bids[1].confidence < 0.10:
            ties.append(r)
    print(f"\n【Tie cases (top-2 信心差 < 0.10, 共 {len(ties)} 條)】")
    for r in ties[:10]:
        bid_str = ", ".join(f"{b.name}={b.confidence:.2f}" for b in r.bids[:3])
        print(f"  q='{r.event.query[:50]}' bids: {bid_str}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    paths = sys.argv[1:]
    print(f"📂 讀 {len(paths)} 個 log 檔案...")
    events, outcomes = parse_log_files(paths)
    print(f"📊 抽到 {len(events)} 條 wake events，{len(outcomes)} 條 outcome markers\n")

    fake_ctrl = _FakeController()
    agents = [MusicAgent(fake_ctrl), NemoClawAgent(fake_ctrl)]

    results: list[ReplayResult] = []
    for ev in events:
        bids, winner = replay_one(ev, agents)
        legacy = find_legacy_outcome(ev, outcomes)
        results.append(ReplayResult(event=ev, bids=bids, winner=winner, legacy=legacy))

    print_report(results)


if __name__ == "__main__":
    main()
