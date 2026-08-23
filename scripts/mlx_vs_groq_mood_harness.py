"""MLX (Qwen2.5-1.5B-Instruct-4bit) vs Groq openai/gpt-oss-20b — mood_sensor 分類器 harness.

Apple Foundation Models 框架跑同一批 mood 語料延遲 3.5s（fm_vs_groq_mood_harness.py），
比 Groq 慢 7-10 倍，被 reject。這隻改用 MLX（自己抓權重跑，不經 Apple guided-generation
框架）測同一批語料，看延遲差是框架問題還是這台 M1 8GB 硬體本身的問題。

使用：
    python scripts/mlx_vs_groq_mood_harness.py [corpus.jsonl]
    GROQ_API_KEY 必須在環境變數。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mood_sensor import MOOD_CLASSIFIER_SYSTEM, parse_mood_label  # noqa: E402
from scripts.fm_vs_groq_mood_harness import call_groq_mood, aggregate_report, MoodRow, _load_corpus  # noqa: E402

logger = logging.getLogger("mlx_mood_harness")

MODEL_ID = os.environ.get("MLX_MOOD_MODEL", "mlx-community/Qwen2.5-3B-Instruct-4bit")


def call_mlx_mood(model, tokenizer, user_prompt: str) -> tuple[Optional[str], int]:
    from mlx_lm import generate

    messages = [
        {"role": "system", "content": MOOD_CLASSIFIER_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    try:
        prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    except Exception:
        # 部分 chat template（如 Gemma-2）不支援獨立 system role，併入 user turn。
        merged = [{"role": "user", "content": f"{MOOD_CLASSIFIER_SYSTEM}\n\n{user_prompt}"}]
        prompt = tokenizer.apply_chat_template(merged, add_generation_prompt=True, tokenize=False)

    start = time.monotonic()
    text = generate(model, tokenizer, prompt=prompt, max_tokens=10, verbose=False)
    dt = int((time.monotonic() - start) * 1000)
    return parse_mood_label(text), dt


async def run_harness(corpus_path: Path, output_dir: Path) -> dict:
    from groq import AsyncGroq
    from mlx_lm import load

    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not set")
    groq_client = AsyncGroq(api_key=groq_key)

    print(f"[harness] loading MLX model {MODEL_ID}...", flush=True)
    t0 = time.monotonic()
    model, tokenizer = load(MODEL_ID)
    print(f"[harness] MLX model loaded in {time.monotonic() - t0:.1f}s (excluded from per-call latency)", flush=True)

    corpus = _load_corpus(corpus_path)
    print(f"[harness] {len(corpus)} samples", flush=True)

    rows: list[MoodRow] = []
    for i, sample in enumerate(corpus, 1):
        raw = sample["raw"]
        tag = sample.get("tag", "")
        expected = sample.get("expected", "")
        user_prompt = "對話片段（5 分鐘窗口）：\n" + raw

        mlx_mood, mlx_lat = call_mlx_mood(model, tokenizer, user_prompt)
        groq_mood, groq_lat = await call_groq_mood(groq_client, user_prompt)

        row = MoodRow(
            tag=tag, expected=expected,
            fm_mood=mlx_mood, groq_mood=groq_mood,
            fm_latency_ms=mlx_lat, groq_latency_ms=groq_lat,
            fm_correct=(mlx_mood == expected),
            groq_correct=(groq_mood == expected),
            agree=(mlx_mood == groq_mood),
        )
        rows.append(row)

        marker = "✓" if row.fm_correct else "✗"
        print(f"  [{i:2d}/{len(corpus)}] {marker} tag={tag} expected={expected} "
              f"mlx={mlx_mood}({mlx_lat}ms) groq={groq_mood}({groq_lat}ms)", flush=True)

    report = aggregate_report(rows)
    ts = time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"mlx_vs_groq_mood_report_{ts}.json"
    json_path.write_text(json.dumps({
        "model": MODEL_ID,
        "summary": report,
        "samples": [asdict(r) for r in rows],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[harness] report written: {json_path}")
    print(f"[harness] verdict: {report['verdict']}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    corpus_arg = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/mood_harness_corpus.jsonl"
    corpus_path = Path(corpus_arg)
    if not corpus_path.is_absolute():
        corpus_path = REPO_ROOT / corpus_path
    if not corpus_path.exists():
        print(f"corpus not found: {corpus_path}", file=sys.stderr)
        return 2
    output_dir = REPO_ROOT / "records"
    asyncio.run(run_harness(corpus_path, output_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
