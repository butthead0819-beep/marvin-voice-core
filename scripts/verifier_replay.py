"""Verifier replay — 把歷史 wake events 過一遍 70B verifier，
看 bid vector + larger context 是否能補位 bus 失敗的判斷。

實驗設計（Phase 2 Q3 measurement-first）：
  1. 從 5/14-5/19 log 抽 wake events（reuse replay_bid_history parser）
  2. 對每條：用 bus 出 bid vector → 拼 verifier prompt → 打 Groq 70B
  3. 比對 verifier_intent vs legacy_kind（ground truth = 真實生產行為）
  4. 統計：verifier 補位多少 bus 失敗 vs 引入多少新失敗

只跑「interesting subset」：bus disagreement + borderline + no_bid 案例
避免燒 TPM 在「bus 跟 legacy 已經一致」的 trivial case 上。

用法：
    python scripts/verifier_replay.py records/daily/*.log
    GROQ_API_KEY 必須在環境變數。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.replay_bid_history import (  # noqa: E402
    OWNER_NAMES,
    LegacyOutcome,
    WakeEvent,
    _FakeController,
    find_legacy_outcome,
    parse_log_files,
    replay_one,
)
from intent_agents.music_agent import MusicAgent  # noqa: E402
from intent_agents.nemoclaw_agent import NemoClawAgent  # noqa: E402

logger = logging.getLogger("verifier_replay")

VALID_INTENTS = frozenset({"music", "nemoclaw", "chat", "drop"})
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)
_FENCE_END_RE = re.compile(r"\n?```\s*.*$", re.DOTALL)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VerifierOutput:
    intent: str
    confidence: float
    reason: str


@dataclass
class VerifierResult:
    query: str
    legacy_kind: str
    bus_winner: str  # "music" / "nemoclaw" / "no_bid"
    verifier_intent: str
    verifier_confidence: float
    verifier_reason: str
    verifier_latency_ms: int
    bid_vector: list[tuple[str, float, str]] = field(default_factory=list)


# ── Pure logic (tested) ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是 Marvin Discord voice bot 的意圖分類驗證器（intent verifier）。

背景：Marvin 是 Discord 語音助理。使用者透過麥克風說話，經過 STT 轉文字
後，由各個 intent agent 出價競標，最高信心 ≥0.30 的接走處理。當 bid 結
果不確定或無人接時（borderline / no_bid），你介入做最終判斷。

你會收到：
- <RawSTT>：原始 STT 文字（可能含錯字、雜訊）
- <Cleaned>：8B LLM 清洗過的版本
- <Speaker>：說話者 Discord 名稱
- <RecentContext>：對話前文（最近幾輪）
- <AgentBids>：各 agent 的出價分數與理由

注意：低 bid 不代表沒線索——它告訴你「該 agent 試過了不太確定」。
你的工作是綜合所有訊號，輸出最終意圖分類。

分類選項（必須四選一）：
- music: 播放/控制音樂的指令
- nemoclaw: 給 NemoClaw（owner 的私人助理，觸發詞「龍蝦」）的查詢
- chat: 對 Marvin 的一般對話/問題（會走主 LLM 回應）
- drop: 非 Marvin 對話、噪音、給其他人說話、無意義碎片（不該回應）

輸出格式：單行 JSON，禁止其他內容
{"intent": "<music|nemoclaw|chat|drop>", "confidence": <0.0-1.0>, "reason": "<簡短中文說明>"}
"""


def build_verifier_user_prompt(
    *,
    raw: str,
    cleaned: str,
    speaker: str,
    bids: list[tuple[str, float, str]],
    recent_context: list[tuple[str, str]],
) -> str:
    """組裝 verifier user message。bids 是 [(name, conf, reason), ...]。"""
    lines = [
        f"<Speaker>{speaker}</Speaker>",
        f"<RawSTT>{raw}</RawSTT>",
        f"<Cleaned>{cleaned}</Cleaned>",
    ]
    if recent_context:
        ctx_str = "\n".join(f"{sp}：{txt}" for sp, txt in recent_context)
        lines.append(f"<RecentContext>\n{ctx_str}\n</RecentContext>")
    if bids:
        bid_lines = [f"  {name}: {conf:.2f} ({reason})" for name, conf, reason in bids]
        lines.append("<AgentBids>\n" + "\n".join(bid_lines) + "\n</AgentBids>")
    else:
        lines.append("<AgentBids>no_bids（所有 agent 都沒出價）</AgentBids>")
    return "\n".join(lines)


def _strip_json_fences(text: str) -> str:
    s = text.strip()
    if not s.startswith("```"):
        return s
    s = _FENCE_RE.sub("", s, count=1)
    s = _FENCE_END_RE.sub("", s, count=1)
    return s.strip()


def parse_verifier_response(text: str) -> Optional[VerifierOutput]:
    if not text:
        return None
    s = _strip_json_fences(text)
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    confidence = data.get("confidence")
    reason = data.get("reason", "")
    if intent not in VALID_INTENTS:
        return None
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None
    if not isinstance(reason, str):
        reason = str(reason)
    conf = max(0.0, min(1.0, float(confidence)))
    return VerifierOutput(intent=intent, confidence=conf, reason=reason)


def classify_match(*, verifier_intent: str, legacy_kind: str) -> str:
    """比對 verifier 分類 vs legacy 實際 outcome。

    回傳：
      "match"       — verifier 與 legacy 結論一致
      "fp_music"    — verifier 說 music 但 legacy 沒播
      "fn_music"    — verifier 沒說 music 但 legacy 播了
      "fp_nemoclaw" — verifier 說 nemoclaw 但 legacy 沒走
      "fn_nemoclaw" — verifier 沒說 nemoclaw 但 legacy 走了
      "other_mismatch" — 其他不一致
    """
    legacy_is_music = legacy_kind.startswith("music_")
    legacy_is_nemo = legacy_kind == "nemoclaw"
    legacy_is_default = legacy_kind == "marvin_or_skip"

    if verifier_intent == "music":
        return "match" if legacy_is_music else "fp_music"
    if verifier_intent == "nemoclaw":
        return "match" if legacy_is_nemo else "fp_nemoclaw"
    # chat / drop
    if legacy_is_default:
        return "match"
    if legacy_is_music:
        return "fn_music"
    if legacy_is_nemo:
        return "fn_nemoclaw"
    return "other_mismatch"


def aggregate_verifier_stats(results: list[VerifierResult]) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0}

    verifier_matches = 0
    bus_matches = 0
    rescued = 0  # bus 錯而 verifier 對
    introduced = 0  # bus 對而 verifier 錯
    match_breakdown = Counter()

    for r in results:
        v_match = classify_match(verifier_intent=r.verifier_intent, legacy_kind=r.legacy_kind)
        b_match = _bus_match(bus_winner=r.bus_winner, legacy_kind=r.legacy_kind)
        match_breakdown[v_match] += 1
        if v_match == "match":
            verifier_matches += 1
        if b_match == "match":
            bus_matches += 1
        if v_match == "match" and b_match != "match":
            rescued += 1
        if v_match != "match" and b_match == "match":
            introduced += 1

    latencies = [r.verifier_latency_ms for r in results]
    latencies.sort()

    return {
        "n": n,
        "verifier_matches": verifier_matches,
        "bus_matches": bus_matches,
        "verifier_rescued_bus_failures": rescued,
        "verifier_introduced_failures": introduced,
        "net_improvement": rescued - introduced,
        "verifier_match_rate": verifier_matches / n,
        "bus_match_rate": bus_matches / n,
        "match_breakdown": dict(match_breakdown),
        "verifier_latency_p50_ms": latencies[len(latencies) // 2] if latencies else 0,
        "verifier_latency_p95_ms": latencies[int(len(latencies) * 0.95)] if latencies else 0,
        "verifier_latency_mean_ms": sum(latencies) // n if latencies else 0,
    }


def _bus_match(*, bus_winner: str, legacy_kind: str) -> str:
    """Bus 對照同邏輯（複用 classify_match 結構但 input 是 bus winner name）。"""
    legacy_is_music = legacy_kind.startswith("music_")
    legacy_is_nemo = legacy_kind == "nemoclaw"
    legacy_is_default = legacy_kind == "marvin_or_skip"

    if bus_winner == "music":
        return "match" if legacy_is_music else "fp_music"
    if bus_winner == "nemoclaw":
        return "match" if legacy_is_nemo else "fp_nemoclaw"
    # no_bid (fall to default)
    if legacy_is_default:
        return "match"
    if legacy_is_music:
        return "fn_music"
    if legacy_is_nemo:
        return "fn_nemoclaw"
    return "other_mismatch"


# ── I/O glue ──────────────────────────────────────────────────────────────────

def _is_interesting(bus_winner: str, bus_bids: list, legacy_kind: str) -> bool:
    """過濾 verify 跑哪些 case，節省 TPM。

    跑：no_bid / borderline winner (<0.65) / bus-legacy disagreement
    跳：bus 跟 legacy 都同意的高信心 case
    """
    legacy_is_music = legacy_kind.startswith("music_")
    legacy_is_nemo = legacy_kind == "nemoclaw"
    legacy_is_default = legacy_kind == "marvin_or_skip"

    if bus_winner == "no_bid":
        return True  # 永遠驗 no_bid
    if bus_bids and bus_bids[0].confidence < 0.65:
        return True  # borderline 也驗

    # 分歧 case
    if bus_winner == "music" and not legacy_is_music:
        return True
    if bus_winner == "nemoclaw" and not legacy_is_nemo:
        return True
    if bus_winner == "music" and legacy_is_default:
        return True
    if bus_winner == "nemoclaw" and legacy_is_default:
        return True

    return False  # bus 高信心 + 跟 legacy 一致 → 不浪費 TPM


async def call_70b_verifier(client, system: str, user: str) -> tuple[Optional[VerifierOutput], int]:
    start = time.monotonic()
    try:
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        dt = int((time.monotonic() - start) * 1000)
        return parse_verifier_response(response.choices[0].message.content), dt
    except Exception as e:
        dt = int((time.monotonic() - start) * 1000)
        logger.warning(f"70B verifier failed: {e}")
        return None, dt


async def run_verifier_replay(log_paths: list[str], output_dir: Path) -> dict:
    print(f"📂 讀 {len(log_paths)} 個 log...", flush=True)
    events, outcomes = parse_log_files(log_paths)
    print(f"📊 抽到 {len(events)} 條 wake events，{len(outcomes)} 條 outcome markers", flush=True)

    fake_ctrl = _FakeController()
    agents = [MusicAgent(fake_ctrl), NemoClawAgent(fake_ctrl)]

    # 第一輪：跑 bus replay 拿 bid vector + legacy
    candidates = []
    for ev in events:
        bids, winner = replay_one(ev, agents)
        legacy = find_legacy_outcome(ev, outcomes)
        bus_winner = winner.name if winner else "no_bid"
        if _is_interesting(bus_winner, bids, legacy.kind):
            candidates.append((ev, bids, winner, legacy))

    print(f"🎯 篩出 {len(candidates)} 條 interesting cases (no_bid + borderline + 分歧)", flush=True)
    print(f"   ({len(events) - len(candidates)} 條 bus+legacy 共識高信心 → 跳過)", flush=True)

    if not candidates:
        print("無 interesting case，結束")
        return {}

    # 第二輪：跑 verifier
    from groq import AsyncGroq
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not set")
    client = AsyncGroq(api_key=groq_key)

    results: list[VerifierResult] = []
    print(f"\n🤖 跑 70B verifier ({len(candidates)} calls)...\n", flush=True)
    for i, (ev, bids, winner, legacy) in enumerate(candidates, 1):
        bid_vector = [(b.name, b.confidence, b.reason) for b in bids]
        user_prompt = build_verifier_user_prompt(
            raw=ev.raw_text, cleaned=ev.query, speaker=ev.speaker,
            bids=bid_vector, recent_context=[],
        )
        vo, latency = await call_70b_verifier(client, SYSTEM_PROMPT, user_prompt)
        if vo is None:
            print(f"  [{i:3d}/{len(candidates)}] ✗ verifier parse fail (latency={latency}ms)", flush=True)
            continue

        bus_winner_name = winner.name if winner else "no_bid"
        result = VerifierResult(
            query=ev.query, legacy_kind=legacy.kind, bus_winner=bus_winner_name,
            verifier_intent=vo.intent, verifier_confidence=vo.confidence,
            verifier_reason=vo.reason, verifier_latency_ms=latency,
            bid_vector=bid_vector,
        )
        results.append(result)

        v_match = classify_match(verifier_intent=vo.intent, legacy_kind=legacy.kind)
        b_match = _bus_match(bus_winner=bus_winner_name, legacy_kind=legacy.kind)
        marker = "✓" if v_match == "match" else "✗"
        flag = ""
        if v_match == "match" and b_match != "match":
            flag = " 🆙RESCUE"
        elif v_match != "match" and b_match == "match":
            flag = " ⚠️INTRODUCED"
        print(f"  [{i:3d}/{len(candidates)}] {marker} bus={bus_winner_name:9s} "
              f"→ verify={vo.intent:7s} (legacy={legacy.kind}){flag} "
              f"q='{ev.query[:35]}' {latency}ms", flush=True)

    stats = aggregate_verifier_stats(results)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"verifier_replay_{ts}.json"
    json_path.write_text(json.dumps({
        "stats": stats,
        "samples": [asdict(r) for r in results],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = output_dir / f"verifier_replay_{ts}.md"
    md_path.write_text(_render_markdown(stats, results), encoding="utf-8")

    print("\n══════════ Summary ══════════")
    print(f"Verifier matches: {stats['verifier_matches']}/{stats['n']} "
          f"({stats['verifier_match_rate']:.1%})")
    print(f"Bus matches:      {stats['bus_matches']}/{stats['n']} "
          f"({stats['bus_match_rate']:.1%})")
    print(f"🆙 Rescued bus failures:    {stats['verifier_rescued_bus_failures']}")
    print(f"⚠️  Introduced new failures: {stats['verifier_introduced_failures']}")
    print(f"Net improvement: {stats['net_improvement']:+d}")
    print(f"Verifier latency p50/p95/mean: "
          f"{stats['verifier_latency_p50_ms']}/{stats['verifier_latency_p95_ms']}/"
          f"{stats['verifier_latency_mean_ms']}ms")
    print(f"\nReport: {json_path}\n        {md_path}")
    return stats


def _render_markdown(stats: dict, results: list[VerifierResult]) -> str:
    lines = [
        "# 70B Verifier Replay Report",
        "",
        "## Summary",
        "",
        f"- Interesting cases: **{stats['n']}**",
        f"- Verifier match rate: **{stats['verifier_match_rate']:.1%}** ({stats['verifier_matches']}/{stats['n']})",
        f"- Bus match rate: **{stats['bus_match_rate']:.1%}** ({stats['bus_matches']}/{stats['n']})",
        f"- 🆙 Rescued bus failures: **{stats['verifier_rescued_bus_failures']}**",
        f"- ⚠️ Introduced new failures: **{stats['verifier_introduced_failures']}**",
        f"- **Net improvement: {stats['net_improvement']:+d}**",
        f"- Verifier latency p50/p95/mean: {stats['verifier_latency_p50_ms']}/{stats['verifier_latency_p95_ms']}/{stats['verifier_latency_mean_ms']}ms",
        "",
        "## Match breakdown",
        "",
    ]
    for k, v in stats["match_breakdown"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Rescued cases (bus 錯而 verifier 對)")
    lines.append("")
    for r in results:
        v_match = classify_match(verifier_intent=r.verifier_intent, legacy_kind=r.legacy_kind)
        b_match = _bus_match(bus_winner=r.bus_winner, legacy_kind=r.legacy_kind)
        if v_match == "match" and b_match != "match":
            bid_str = ", ".join(f"{n}={c:.2f}" for n, c, _ in r.bid_vector) or "(空)"
            lines.append(f"- `{r.query[:60]}` → bus={r.bus_winner} (legacy={r.legacy_kind}); "
                         f"verifier={r.verifier_intent} ({r.verifier_confidence:.2f}) "
                         f"reason='{r.verifier_reason[:80]}' bids=[{bid_str}]")
    lines.append("")
    lines.append("## Introduced failures (bus 對而 verifier 錯)")
    lines.append("")
    for r in results:
        v_match = classify_match(verifier_intent=r.verifier_intent, legacy_kind=r.legacy_kind)
        b_match = _bus_match(bus_winner=r.bus_winner, legacy_kind=r.legacy_kind)
        if v_match != "match" and b_match == "match":
            bid_str = ", ".join(f"{n}={c:.2f}" for n, c, _ in r.bid_vector) or "(空)"
            lines.append(f"- `{r.query[:60]}` → bus={r.bus_winner} (legacy={r.legacy_kind}); "
                         f"verifier={r.verifier_intent} ({r.verifier_confidence:.2f}) "
                         f"reason='{r.verifier_reason[:80]}' bids=[{bid_str}]")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    log_paths = sys.argv[1:]
    output_dir = REPO_ROOT / "records"
    asyncio.run(run_verifier_replay(log_paths, output_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
