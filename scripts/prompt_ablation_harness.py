"""Prompt ablation — compare cleaner output with vs without 「強制映射規則」section.

5/19 context sweep 發現 injection 主要源頭是 system prompt 的喚醒詞強制映射太激進
（「麻煩」→「麻文」→「馬文」誤觸發）。本實驗砍掉那段，量化品質影響：

  - injection_rate (over-correction): 應該降
  - legitimate_wake_recall: 砍規則後「麻文」「阿文」這類真實音近誤判是否還能修對？
  - token_cost: 省 system prompt 大小（~50 tokens）
  - wake_decision agreement vs baseline

用 5/19 context sweep 同一批 40 corpus + N=5 baseline 配置，只變 system prompt。

用法：
    python scripts/prompt_ablation_harness.py [corpus.jsonl] [--sample 40]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.context_sweep_harness import (  # noqa: E402
    is_wake_injection_hallucination,
    trim_context,
    _call_cleaner,
    _interpret_wake,
    _load_corpus,
)

logger = logging.getLogger("prompt_ablation")

# 強制映射 section 的精確邊界（line 357-361 in marvin_prompts.py stt_cleaner prompt）
FORCED_MAPPING_RE = re.compile(
    r"【喚醒詞強制映射規則】.*?禁止在句中任何位置添加「馬文」。禁止將非開頭位置的音近詞替換。\n",
    re.DOTALL,
)

# v3: 保留 wake word 中文錨點，只砍激進的音近詞清單
# replacement 仍守住「禁止在句中任何位置添加」與「禁止非開頭替換」兩條安全閥
V3_REPLACEMENT = (
    "【喚醒詞】Marvin 的中文名是「馬文」。文字中出現「馬文」或音同「馬文」者視為喚醒詞。\n"
    "禁止在句中任何位置額外添加「馬文」。禁止將非開頭位置的音近詞替換為「馬文」。\n"
)

# v4 = v3 + 英文 Marvin 翻譯規則（v3 在 5/19 ablation 上唯一弱項：
# 「Marvin, we are very」「Marvin, ivan」這類英文開頭 cleaner 不翻成「馬文」→ intent=0）
V4_REPLACEMENT = (
    "【喚醒詞】Marvin 的中文名是「馬文」。文字中出現「馬文」或音同「馬文」者視為喚醒詞。\n"
    "若 <Target> 以英文 \"Marvin\" 開頭，cleaned 應將「Marvin」改寫成「馬文」並視為喚醒。\n"
    "禁止在句中任何位置額外添加「馬文」。禁止將非開頭位置的音近詞替換為「馬文」。\n"
)


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class AblationRow:
    variant: str           # "baseline" or "no_forced_mapping"
    raw: str
    cleaned: str
    tokens: int
    wake_intent: float
    is_wake: bool
    injection: bool        # raw 無喚醒詞但 cleaned 有 → 注入
    latency_ms: int


# ── Pure logic ────────────────────────────────────────────────────────────────

def strip_forced_mapping(system_prompt: str) -> str:
    """Remove the 「強制映射規則」 section from cleaner system prompt."""
    return FORCED_MAPPING_RE.sub("", system_prompt, count=1)


def replace_with_anchor(system_prompt: str) -> str:
    """v3: 砍激進音近詞清單，但保留「馬文 = wake word」中文錨點 + 兩條禁止規則。"""
    return FORCED_MAPPING_RE.sub(V3_REPLACEMENT, system_prompt, count=1)


def replace_with_v4(system_prompt: str) -> str:
    """v4: v3 + 英文 Marvin → 馬文 翻譯規則。"""
    return FORCED_MAPPING_RE.sub(V4_REPLACEMENT, system_prompt, count=1)


def is_legitimate_wake_recall(*, raw: str, cleaned: str) -> bool:
    """raw 包含明確音近誤判詞（在強制映射清單），cleaned 修正成「馬文」=合理 recall。

    對「砍掉規則會不會傷真實 wake 修正」這個 metric。
    """
    # 從 prompt 抓的音近詞清單（手動 sync）
    phonetic_typos = ("麻文", "馬門", "馬萌", "馬溫", "馬穩", "碼文",
                      "媽問", "媽們", "罵文", "艾馬文", "艾瑪文",
                      "阿姨文", "阿姨", "阿萌", "阿文", "雅文")
    raw_first3 = raw.strip()[:3]
    if not any(typo in raw_first3 for typo in phonetic_typos):
        return False
    return "馬文" in cleaned


def aggregate_ablation(rows: list[AblationRow]) -> dict:
    if not rows:
        return {}

    by_variant: dict[str, list[AblationRow]] = defaultdict(list)
    for r in rows:
        by_variant[r.variant].append(r)

    # Per-raw map for cross-variant wake decision comparison
    baseline_wake = {r.raw: r.is_wake for r in by_variant.get("baseline", [])}
    baseline_clean = {r.raw: r.cleaned for r in by_variant.get("baseline", [])}

    out = {"by_variant": {}}
    for variant, rs in by_variant.items():
        injections = sum(1 for r in rs if r.injection)
        wake_recalls = sum(1 for r in rs if is_legitimate_wake_recall(raw=r.raw, cleaned=r.cleaned))
        # 怎麼算 legit wake denominator? 用 raw 有音近詞的數量
        eligible = sum(1 for r in rs
                       if any(t in r.raw[:3]
                              for t in ("麻文", "馬門", "馬萌", "馬溫", "馬穩",
                                        "碼文", "媽問", "媽們", "罵文",
                                        "艾馬文", "艾瑪文", "阿姨文", "阿姨",
                                        "阿萌", "阿文", "雅文")))
        flips = 0
        cleaned_diffs = 0
        if variant != "baseline":
            for r in rs:
                if r.raw in baseline_wake and r.is_wake != baseline_wake[r.raw]:
                    flips += 1
                if r.raw in baseline_clean and r.cleaned != baseline_clean[r.raw]:
                    cleaned_diffs += 1

        entry = {
            "n_samples": len(rs),
            "mean_tokens": sum(r.tokens for r in rs) // len(rs),
            "injection_count": injections,
            "injection_rate": injections / len(rs),
            "wake_recall_count": wake_recalls,
            "wake_recall_eligible": eligible,
            "wake_recall_rate": (wake_recalls / eligible) if eligible else None,
            "wake_rate": sum(1 for r in rs if r.is_wake) / len(rs),
            "mean_latency_ms": sum(r.latency_ms for r in rs) // len(rs),
        }
        if variant != "baseline":
            entry["wake_flip_count"] = flips
            entry["cleaned_diff_count"] = cleaned_diffs
        out["by_variant"][variant] = entry
    return out


# ── I/O glue ──────────────────────────────────────────────────────────────────

async def run_ablation(corpus_path: Path, sample_size: int, output_dir: Path) -> dict:
    from marvin_prompts import PromptManager
    pm = PromptManager()
    baseline_prompt = pm.get_instruction("stt_cleaner", vision_enabled=False)
    stripped_prompt = strip_forced_mapping(baseline_prompt)
    v3_prompt = replace_with_anchor(baseline_prompt)
    v4_prompt = replace_with_v4(baseline_prompt)

    if len(stripped_prompt) >= len(baseline_prompt):
        raise RuntimeError("FORCED_MAPPING_RE didn't match baseline prompt")
    print(f"[ablation] baseline: {len(baseline_prompt)} chars, "
          f"stripped: {len(stripped_prompt)} chars, "
          f"v3_anchor: {len(v3_prompt)} chars, "
          f"v4_anchor_en: {len(v4_prompt)} chars", flush=True)

    from groq import AsyncGroq
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not set")
    client = AsyncGroq(api_key=groq_key)

    corpus = _load_corpus(corpus_path)
    if sample_size and sample_size < len(corpus):
        rng = random.Random(20260519)  # SAME seed as context sweep for apples-to-apples
        corpus = rng.sample(corpus, sample_size)
    print(f"[ablation] {len(corpus)} samples × 2 variants = {len(corpus) * 2} calls", flush=True)

    all_rows: list[AblationRow] = []
    for i, sample in enumerate(corpus, 1):
        raw = sample["raw"]
        prior = sample.get("prior_context", [])
        ctx = trim_context(prior, 5)  # N=5 matches prod default

        for variant, system in [("baseline", baseline_prompt),
                                ("no_forced_mapping", stripped_prompt),
                                ("v3_anchor", v3_prompt),
                                ("v4_anchor_en", v4_prompt)]:
            parsed, tokens, lat = await _call_cleaner(client, system, raw, ctx)
            if parsed is None:
                print(f"  [{i:2d}/{len(corpus)} {variant}] parse fail", flush=True)
                continue
            cleaned = str(parsed.get("cleaned", "")).strip() or raw
            intent, is_wake = _interpret_wake(parsed)
            injection = is_wake_injection_hallucination(raw=raw, cleaned=cleaned)
            row = AblationRow(
                variant=variant, raw=raw, cleaned=cleaned, tokens=tokens,
                wake_intent=intent, is_wake=is_wake, injection=injection,
                latency_ms=lat,
            )
            all_rows.append(row)
            marker = "💉" if injection else ("✓" if is_wake else "·")
            print(f"  [{i:2d}/{len(corpus)} {variant:18s}] {marker} tok={tokens:4d} "
                  f"intent={intent:.2f} raw='{raw[:25]}' → '{cleaned[:25]}'", flush=True)

    summary = aggregate_ablation(all_rows)
    ts = time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"prompt_ablation_{ts}.json"
    json_path.write_text(json.dumps({
        "summary": summary,
        "rows": [r.__dict__ for r in all_rows],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = output_dir / f"prompt_ablation_{ts}.md"
    md_path.write_text(_render_md(summary, all_rows), encoding="utf-8")

    print(f"\n[ablation] report:\n  {json_path}\n  {md_path}")
    _print_summary(summary)
    return summary


def _print_summary(summary: dict):
    print("\n══════ Prompt Ablation Summary ══════")
    print(f"{'variant':>22} {'samples':>8} {'tok':>6} {'inj_rate':>9} "
          f"{'recall':>13} {'flip':>5} {'diff':>5}")
    for variant in ("baseline", "no_forced_mapping", "v3_anchor", "v4_anchor_en"):
        e = summary["by_variant"].get(variant, {})
        if not e:
            continue
        recall = e.get("wake_recall_rate")
        recall_str = f"{recall:.0%}" if recall is not None else "—"
        recall_full = f"{e['wake_recall_count']}/{e['wake_recall_eligible']} ({recall_str})"
        flip = e.get("wake_flip_count", "—")
        diff = e.get("cleaned_diff_count", "—")
        print(f"{variant:>22} {e['n_samples']:>8} {e['mean_tokens']:>6} "
              f"{e['injection_rate']:>9.1%} {recall_full:>13} {str(flip):>5} {str(diff):>5}")


def _render_md(summary: dict, rows: list[AblationRow]) -> str:
    lines = [
        "# Prompt Ablation Report",
        "",
        "Baseline = current `stt_cleaner` prompt; no_forced_mapping = same prompt with",
        "「喚醒詞強制映射規則」section removed.",
        "",
        "## Variant comparison",
        "",
        "| variant | samples | mean_tok | inj_rate | wake_recall | wake_flip | cleaned_diff |",
        "|---|---|---|---|---|---|---|",
    ]
    for variant in ("baseline", "no_forced_mapping", "v3_anchor", "v4_anchor_en"):
        e = summary["by_variant"].get(variant, {})
        if not e:
            continue
        recall = e.get("wake_recall_rate")
        recall_str = f"{recall:.0%}" if recall is not None else "—"
        recall_full = f"{e['wake_recall_count']}/{e['wake_recall_eligible']} ({recall_str})"
        flip = e.get("wake_flip_count", "—")
        diff = e.get("cleaned_diff_count", "—")
        lines.append(f"| {variant} | {e['n_samples']} | {e['mean_tokens']} | "
                     f"{e['injection_rate']:.1%} | {recall_full} | {flip} | {diff} |")
    lines.append("")

    # Per-raw comparison across all variants
    variant_order = ("baseline", "no_forced_mapping", "v3_anchor", "v4_anchor_en")
    by_raw = {v: {r.raw: r for r in rows if r.variant == v} for v in variant_order}

    def _tag(r):
        return "💉" if r.injection else ("✓" if r.is_wake else "·")

    lines.append("## Per-raw comparison (only rows with disagreement)")
    lines.append("")
    for raw in by_raw["baseline"]:
        variant_rows = {v: by_raw[v].get(raw) for v in variant_order}
        if any(r is None for r in variant_rows.values()):
            continue
        b = variant_rows["baseline"]
        if all(r.cleaned == b.cleaned and r.is_wake == b.is_wake
               for r in variant_rows.values()):
            continue
        lines.append(f"- raw=`{raw[:50]}`")
        for v in variant_order:
            r = variant_rows[v]
            lines.append(f"  - {_tag(r)} {v:18s} → `{r.cleaned[:50]}` (intent={r.wake_intent:.2f})")
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

    asyncio.run(run_ablation(corpus_path, args.sample, REPO_ROOT / "records"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
