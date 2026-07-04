"""Context size sweep — 量 Groq 8b cleaner 在不同 prior context N 下的：
  - token cost (TPM 節省潛力)
  - 過矯正幻覺率 (Wake Injection Guard 命中：cleaner 注入 raw 沒有的喚醒詞)
  - cleaned text 變動 (是否與 baseline N=5 一致)
  - wake decision 翻轉率 (對同一 raw，N 改變是否導致 is_wake 翻轉)

實驗目的（Phase 2 Q2）：找 N 的甜蜜點 — 5/18 TPM 痛點救援。

用法：
    python scripts/context_sweep_harness.py [corpus.jsonl] [--sample N]
    GROQ_API_KEY 必須在環境變數。

預設 corpus: tests/fixtures/context_sweep_corpus.jsonl
預設 sample: 40
context sizes: [0, 2, 5, 10]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils import check_cleaned_text_for_wake  # noqa: E402

logger = logging.getLogger("context_sweep")

CONTEXT_SIZES = [0, 2, 5, 10]
BASELINE_N = 5
WAKE_THRESHOLD = 0.70  # 對齊 stt_cleaner


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class SweepRow:
    n_context: int
    raw: str
    cleaned: str
    tokens: int
    wake_intent: float
    is_wake: bool
    injection: bool
    latency_ms: int


@dataclass
class SweepRunResult:
    n: int
    rows: list[SweepRow] = field(default_factory=list)


# ── Pure logic (tested) ───────────────────────────────────────────────────────

def trim_context(ctx: list[dict], n: int) -> list[dict]:
    """取 prior_context 最近 N 條（chronological 結尾）。"""
    if n <= 0:
        return []
    return list(ctx[-n:])


def is_wake_injection_hallucination(*, raw: str, cleaned: str) -> bool:
    """Cleaner 過矯正：raw 完全無喚醒詞/音近詞，但 cleaned 有「馬文」→ 注入幻覺。

    對齊 stt_cleaner._verify_wake_against_raw 的核心邏輯，但不依賴
    GeminiRouter 內部 state — 只純函式檢查 raw 與 cleaned。
    """
    raw_has_wake = check_cleaned_text_for_wake(raw)
    cleaned_has_wake = check_cleaned_text_for_wake(cleaned)
    if not cleaned_has_wake:
        return False
    if raw_has_wake:
        return False
    # cleaned 有「馬文」但 raw 沒喚醒詞 → 注入
    return True


def aggregate_sweep_results(rows: list[SweepRow], baseline_n: int = BASELINE_N) -> dict:
    if not rows:
        return {"by_n": {}}

    by_n: dict[int, list[SweepRow]] = defaultdict(list)
    for r in rows:
        by_n[r.n_context].append(r)

    # Build per-raw baseline lookup (raw → baseline is_wake)
    baseline_wake: dict[str, bool] = {}
    for r in by_n.get(baseline_n, []):
        baseline_wake[r.raw] = r.is_wake

    out: dict = {"by_n": {}}
    for n, rs in by_n.items():
        tokens = [r.tokens for r in rs]
        latencies = [r.latency_ms for r in rs]
        injections = sum(1 for r in rs if r.injection)
        flips = 0
        flip_total = 0
        if n != baseline_n:
            for r in rs:
                if r.raw in baseline_wake:
                    flip_total += 1
                    if r.is_wake != baseline_wake[r.raw]:
                        flips += 1

        entry = {
            "n_samples": len(rs),
            "mean_tokens": int(statistics.mean(tokens)) if tokens else 0,
            "median_tokens": int(statistics.median(tokens)) if tokens else 0,
            "p95_tokens": int(sorted(tokens)[int(len(tokens) * 0.95)]) if tokens else 0,
            "injection_rate": injections / len(rs),
            "injection_count": injections,
            "mean_latency_ms": int(statistics.mean(latencies)) if latencies else 0,
            "wake_rate": sum(1 for r in rs if r.is_wake) / len(rs),
        }
        if n != baseline_n and flip_total > 0:
            entry["wake_flip_vs_baseline"] = flips / flip_total
            entry["wake_flip_count"] = flips
        elif n == baseline_n:
            entry["wake_flip_vs_baseline"] = 0.0
        out["by_n"][n] = entry

    return out


# ── I/O glue ──────────────────────────────────────────────────────────────────

async def _call_cleaner(client, system: str, raw: str, ctx: list[dict]) -> tuple[Optional[dict], int, int]:
    """Call Groq 8b cleaner with given context. Returns (parsed_json, tokens, latency_ms)."""
    if ctx:
        background = "\n".join(f"{u['speaker']}：{u['text']}" for u in ctx)
        user_message = f"<Background>\n{background}\n</Background>\n\n<Target>{raw}</Target>"
    else:
        user_message = f"<Target>{raw}</Target>"

    start = time.monotonic()
    try:
        response = await client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        dt = int((time.monotonic() - start) * 1000)
        content = response.choices[0].message.content
        tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        try:
            data = json.loads(content.strip())
        except (json.JSONDecodeError, ValueError):
            return None, tokens, dt
        return data, tokens, dt
    except Exception as e:
        dt = int((time.monotonic() - start) * 1000)
        logger.warning(f"Groq call failed: {e}")
        return None, 0, dt


def _interpret_wake(parsed: dict) -> tuple[float, bool]:
    """從 cleaner 輸出取 wake_intent + 算 is_wake。對齊 stt_cleaner 的閾值邏輯。"""
    intent = parsed.get("intent")
    calling = parsed.get("calling", False)
    if not isinstance(intent, (int, float)) or isinstance(intent, bool):
        return 0.0, False
    intent_f = max(0.0, min(1.0, float(intent)))
    if intent_f >= 0.75:
        is_wake = True
    elif intent_f >= 0.65:
        is_wake = bool(calling)
    else:
        is_wake = False
    return intent_f, is_wake


def _load_corpus(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


async def run_sweep(corpus_path: Path, sample_size: int, output_dir: Path) -> dict:
    from marvin_prompts import PromptManager
    pm = PromptManager()
    system_prompt = pm.get_instruction("stt_cleaner", vision_enabled=False)

    from groq import AsyncGroq
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not set")
    client = AsyncGroq(api_key=groq_key)

    corpus = _load_corpus(corpus_path)
    if sample_size and sample_size < len(corpus):
        rng = random.Random(20260519)  # deterministic seed
        corpus = rng.sample(corpus, sample_size)
    print(f"[sweep] {len(corpus)} samples × {len(CONTEXT_SIZES)} context sizes = "
          f"{len(corpus) * len(CONTEXT_SIZES)} calls", flush=True)

    all_rows: list[SweepRow] = []
    parse_failures = 0
    for i, sample in enumerate(corpus, 1):
        raw = sample["raw"]
        prior = sample.get("prior_context", [])
        for n in CONTEXT_SIZES:
            ctx = trim_context(prior, n)
            parsed, tokens, lat = await _call_cleaner(client, system_prompt, raw, ctx)
            if parsed is None:
                parse_failures += 1
                continue
            cleaned = str(parsed.get("cleaned", "")).strip() or raw
            intent, is_wake = _interpret_wake(parsed)
            injection = is_wake_injection_hallucination(raw=raw, cleaned=cleaned)
            row = SweepRow(
                n_context=n, raw=raw, cleaned=cleaned, tokens=tokens,
                wake_intent=intent, is_wake=is_wake, injection=injection,
                latency_ms=lat,
            )
            all_rows.append(row)
            marker = "💉" if injection else ("✓" if is_wake else "·")
            print(f"  [{i:2d}/{len(corpus)} N={n:2d}] {marker} tok={tokens:3d} "
                  f"intent={intent:.2f} raw='{raw[:25]}' → '{cleaned[:25]}'", flush=True)

    print(f"\n[sweep] parse failures: {parse_failures}", flush=True)

    summary = aggregate_sweep_results(all_rows)
    ts = time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"context_sweep_{ts}.json"
    json_path.write_text(json.dumps({
        "summary": summary,
        "parse_failures": parse_failures,
        "rows": [r.__dict__ for r in all_rows],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = output_dir / f"context_sweep_{ts}.md"
    md_path.write_text(_render_md(summary, all_rows, parse_failures), encoding="utf-8")

    print(f"\n[sweep] report:\n  {json_path}\n  {md_path}")
    _print_summary_table(summary)
    return summary


def _print_summary_table(summary: dict):
    print("\n══════ Context Size Sweep Summary ══════")
    print(f"{'N':>4} {'samples':>8} {'mean_tok':>9} {'p95_tok':>8} "
          f"{'inj_rate':>9} {'flip_vs_baseline':>17} {'mean_lat':>9}")
    for n in sorted(summary["by_n"].keys()):
        e = summary["by_n"][n]
        flip = e.get("wake_flip_vs_baseline")
        flip_str = f"{flip:.1%}" if flip is not None else "—"
        print(f"{n:>4} {e['n_samples']:>8} {e['mean_tokens']:>9} {e['p95_tokens']:>8} "
              f"{e['injection_rate']:>9.1%} {flip_str:>17} {e['mean_latency_ms']:>7}ms")


def _render_md(summary: dict, rows: list[SweepRow], parse_failures: int) -> str:
    lines = [
        "# Context Size Sweep Report",
        "",
        f"- Parse failures: {parse_failures}",
        "",
        "## Per-N stats",
        "",
        f"| N | samples | mean_tok | p95_tok | inj_rate | wake_flip_vs_N={BASELINE_N} | mean_lat |",
        f"|---|---|---|---|---|---|---|",
    ]
    for n in sorted(summary["by_n"].keys()):
        e = summary["by_n"][n]
        flip = e.get("wake_flip_vs_baseline")
        flip_str = f"{flip:.1%}" if flip is not None else "—"
        lines.append(f"| {n} | {e['n_samples']} | {e['mean_tokens']} | {e['p95_tokens']} | "
                     f"{e['injection_rate']:.1%} | {flip_str} | {e['mean_latency_ms']}ms |")
    lines.append("")

    # Injections breakdown
    lines.append("## Injection cases (cleaner 注入 raw 沒有的喚醒詞)")
    lines.append("")
    seen_raw = set()
    for r in rows:
        if not r.injection:
            continue
        key = (r.n_context, r.raw)
        if key in seen_raw:
            continue
        seen_raw.add(key)
        lines.append(f"- N={r.n_context} `{r.raw[:40]}` → `{r.cleaned[:40]}`")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", nargs="?", default="tests/fixtures/context_sweep_corpus.jsonl")
    parser.add_argument("--sample", type=int, default=40)
    args = parser.parse_args()

    corpus_path = Path(args.corpus)
    if not corpus_path.is_absolute():
        corpus_path = REPO_ROOT / corpus_path
    if not corpus_path.exists():
        print(f"corpus not found: {corpus_path}", file=sys.stderr)
        return 2

    asyncio.run(run_sweep(corpus_path, args.sample, REPO_ROOT / "records"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
