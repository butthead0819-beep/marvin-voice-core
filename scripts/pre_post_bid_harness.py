"""Pre+Post cleaner bid — bid on raw text AND on cleaned text，
compare against legacy outcome (ground truth from logs).

Q3 verifier_replay 發現 verifier 拿到的 bid vector 全是空，因為 cleaner 改寫
後 raw 訊號丟失。本實驗測：

  - cleaned_only: 現況——只在 cleaned 上 bid
  - raw_only: 只在 raw 上 bid（不跑 cleaner）
  - combined: raw + cleaned 都 bid，max(confidence) 贏

如果 combined > cleaned_only 顯著，證明 raw 帶有 cleaner 會丟失的 intent 訊號。
而 bid 是 sync ≤5ms，加 raw_bid 幾乎零成本。

純 bid + log replay，不打 LLM。Groq quota 完全不需要。

用法：
    python scripts/pre_post_bid_harness.py records/daily/*.log [bot_main.log*]
"""
from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from intent_bus import Bid, IntentContext  # noqa: E402
from intent_agents.music_agent import MusicAgent  # noqa: E402
from intent_agents.nemoclaw_agent import NemoClawAgent  # noqa: E402
from scripts.replay_bid_history import (  # noqa: E402
    OWNER_NAMES,
    _FakeController,
    find_legacy_outcome,
    parse_log_files,
)

logger = logging.getLogger("pre_post_bid")


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class PrePostRow:
    query: str
    legacy: str
    raw_winner: str             # "music" / "nemoclaw" / "no_bid"
    cleaned_winner: str
    combined_winner: str
    raw_bids: list = field(default_factory=list)
    cleaned_bids: list = field(default_factory=list)


# ── Pure logic (tested) ───────────────────────────────────────────────────────

def combine_bids(*, raw: Optional[Bid], cleaned: Optional[Bid]) -> Optional[Bid]:
    """Pick the higher-confidence bid between raw and cleaned versions."""
    if raw is None:
        return cleaned
    if cleaned is None:
        return raw
    return raw if raw.confidence >= cleaned.confidence else cleaned


def classify_outcome(*, winner_name: str, legacy_kind: str) -> str:
    """Return: match / fp / fn / wrong_agent."""
    legacy_is_music = legacy_kind.startswith("music_")
    legacy_is_nemo = legacy_kind == "nemoclaw"
    legacy_is_default = legacy_kind == "marvin_or_skip"

    if winner_name == "music":
        if legacy_is_music:
            return "match"
        if legacy_is_default:
            return "fp"
        return "wrong_agent"
    if winner_name == "nemoclaw":
        if legacy_is_nemo:
            return "match"
        if legacy_is_default:
            return "fp"
        return "wrong_agent"
    # no_bid (= default)
    if legacy_is_default:
        return "match"
    return "fn"


def aggregate_pre_post_stats(rows: list[PrePostRow]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}

    def _rate(name: str) -> float:
        return sum(1 for r in rows
                   if classify_outcome(winner_name=getattr(r, name), legacy_kind=r.legacy) == "match") / n

    def _breakdown(name: str) -> dict:
        c = Counter()
        for r in rows:
            c[classify_outcome(winner_name=getattr(r, name), legacy_kind=r.legacy)] += 1
        return dict(c)

    return {
        "n": n,
        "cleaned_only_match_rate": _rate("cleaned_winner"),
        "raw_only_match_rate": _rate("raw_winner"),
        "combined_match_rate": _rate("combined_winner"),
        "cleaned_breakdown": _breakdown("cleaned_winner"),
        "raw_breakdown": _breakdown("raw_winner"),
        "combined_breakdown": _breakdown("combined_winner"),
    }


# ── I/O glue ──────────────────────────────────────────────────────────────────

def _bid_with_query(agents, base_ctx: IntentContext, query: str) -> tuple[list[Bid], Optional[Bid]]:
    """Bid with override query. Returns (all_bids, winner)."""
    ctx = IntentContext(
        speaker=base_ctx.speaker,
        raw_text=base_ctx.raw_text,
        query=query,
        original_raw=base_ctx.original_raw,
        wake_intent=base_ctx.wake_intent,
        stream_active=base_ctx.stream_active,
        game_mode=base_ctx.game_mode,
        is_owner=base_ctx.is_owner,
        now=base_ctx.now,
    )
    bids = []
    for agent in agents:
        try:
            b = agent.bid(ctx)
            if b is not None:
                bids.append(b)
        except Exception:
            pass
    bids.sort(key=lambda b: b.confidence, reverse=True)
    winner = bids[0] if bids and bids[0].confidence >= 0.30 else None
    return bids, winner


def run_pre_post(log_paths: list[str], output_dir: Path) -> dict:
    print(f"📂 讀 {len(log_paths)} 個 log...", flush=True)
    events, outcomes = parse_log_files(log_paths)
    print(f"📊 抽到 {len(events)} 條 wake events", flush=True)

    fake_ctrl = _FakeController()
    agents = [MusicAgent(fake_ctrl), NemoClawAgent(fake_ctrl)]

    rows: list[PrePostRow] = []
    timings = {"raw": [], "cleaned": []}

    for ev in events:
        legacy = find_legacy_outcome(ev, outcomes)
        base_ctx = IntentContext(
            speaker=ev.speaker,
            raw_text=ev.raw_text,
            query=ev.query,
            original_raw=ev.raw_text,
            wake_intent=ev.wake_intent,
            stream_active=False,
            game_mode=False,
            is_owner=ev.speaker in OWNER_NAMES,
            now=0.0,
        )

        # Raw bid
        t0 = time.perf_counter()
        raw_bids, raw_winner = _bid_with_query(agents, base_ctx, ev.raw_text)
        timings["raw"].append((time.perf_counter() - t0) * 1000)

        # Cleaned bid (current bus behavior)
        t0 = time.perf_counter()
        cleaned_bids, cleaned_winner = _bid_with_query(agents, base_ctx, ev.query)
        timings["cleaned"].append((time.perf_counter() - t0) * 1000)

        # Combined: max(raw_winner, cleaned_winner) by confidence
        combined = combine_bids(raw=raw_winner, cleaned=cleaned_winner)

        rows.append(PrePostRow(
            query=ev.query, legacy=legacy.kind,
            raw_winner=raw_winner.name if raw_winner else "no_bid",
            cleaned_winner=cleaned_winner.name if cleaned_winner else "no_bid",
            combined_winner=combined.name if combined else "no_bid",
            raw_bids=[(b.name, b.confidence, b.reason) for b in raw_bids],
            cleaned_bids=[(b.name, b.confidence, b.reason) for b in cleaned_bids],
        ))

    stats = aggregate_pre_post_stats(rows)
    stats["raw_bid_mean_ms"] = sum(timings["raw"]) / len(timings["raw"]) if timings["raw"] else 0
    stats["cleaned_bid_mean_ms"] = sum(timings["cleaned"]) / len(timings["cleaned"]) if timings["cleaned"] else 0

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"pre_post_bid_{ts}.json"
    json_path.write_text(json.dumps({
        "stats": stats,
        "rows": [asdict(r) for r in rows],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = output_dir / f"pre_post_bid_{ts}.md"
    md_path.write_text(_render_md(stats, rows), encoding="utf-8")

    _print_summary(stats)
    print(f"\nReport: {json_path}\n        {md_path}")
    return stats


def _print_summary(stats: dict):
    print("\n══════ Pre+Post Bid Summary ══════")
    print(f"n = {stats['n']}")
    print(f"raw_bid mean latency:     {stats['raw_bid_mean_ms']:.3f}ms")
    print(f"cleaned_bid mean latency: {stats['cleaned_bid_mean_ms']:.3f}ms")
    print()
    print(f"{'strategy':>18} {'match_rate':>11} {'breakdown'}")
    for name in ("cleaned_only", "raw_only", "combined"):
        rate = stats[f"{name}_match_rate"]
        breakdown = stats[f"{name.replace('_only', '').replace('_match_rate', '')}_breakdown" if name != "combined" else "combined_breakdown"]
        breakdown_str = ", ".join(f"{k}={v}" for k, v in sorted(breakdown.items()))
        print(f"{name:>18} {rate:>11.1%}  {breakdown_str}")


def _render_md(stats: dict, rows: list[PrePostRow]) -> str:
    lines = [
        "# Pre+Post Bid Report",
        "",
        f"- Wake events: **{stats['n']}**",
        f"- raw_bid mean latency: {stats['raw_bid_mean_ms']:.3f}ms",
        f"- cleaned_bid mean latency: {stats['cleaned_bid_mean_ms']:.3f}ms",
        "",
        "## Strategy comparison",
        "",
        "| strategy | match_rate | match | fp | fn | wrong_agent |",
        "|---|---|---|---|---|---|",
    ]
    for name in ("cleaned_only", "raw_only", "combined"):
        rate = stats[f"{name}_match_rate"]
        bd_key = "combined_breakdown" if name == "combined" else (name.replace("_only", "_breakdown"))
        bd = stats[bd_key]
        lines.append(f"| {name} | {rate:.1%} | {bd.get('match', 0)} | "
                     f"{bd.get('fp', 0)} | {bd.get('fn', 0)} | {bd.get('wrong_agent', 0)} |")
    lines.append("")

    # 分歧 cases: raw vs cleaned disagree
    lines.append("## Cases where raw and cleaned disagree")
    lines.append("")
    seen = set()
    for r in rows:
        if r.raw_winner == r.cleaned_winner:
            continue
        if r.query in seen:
            continue
        seen.add(r.query)
        raw_legacy_match = "✓" if classify_outcome(winner_name=r.raw_winner, legacy_kind=r.legacy) == "match" else "✗"
        cleaned_legacy_match = "✓" if classify_outcome(winner_name=r.cleaned_winner, legacy_kind=r.legacy) == "match" else "✗"
        lines.append(f"- `{r.query[:50]}` (legacy={r.legacy})")
        lines.append(f"  - {raw_legacy_match} raw={r.raw_winner} bids={r.raw_bids}")
        lines.append(f"  - {cleaned_legacy_match} cleaned={r.cleaned_winner} bids={r.cleaned_bids}")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    log_paths = sys.argv[1:]
    output_dir = REPO_ROOT / "records"
    run_pre_post(log_paths, output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
