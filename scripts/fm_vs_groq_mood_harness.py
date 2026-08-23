"""Apple Foundation Model vs Groq openai/gpt-oss-20b — mood_sensor 分類器 harness.

跑同一組多人對話片段 → 分別打 FM CLI daemon 與 Groq 8b → 對照 mood label/延遲。
mood_sensor.py 的輸出是單一詞（無 JSON schema），刻意用來跟 fm_vs_groq_harness.py
的 cleaner（guided JSON，FM p95 14.9s 被拒）對照：慢是 guided-JSON decoding 特有，
還是 FM 引擎本身普遍慢。

使用：
    python scripts/fm_vs_groq_mood_harness.py [corpus.jsonl]
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
from scripts.fm_vs_groq_harness import FMDaemon  # noqa: E402

logger = logging.getLogger("fm_mood_harness")


@dataclass
class MoodRow:
    tag: str
    expected: str
    fm_mood: Optional[str]
    groq_mood: Optional[str]
    fm_latency_ms: int
    groq_latency_ms: int
    fm_correct: bool
    groq_correct: bool
    agree: bool


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def aggregate_report(rows: list[MoodRow]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "verdict": "empty"}

    fm_lat = [r.fm_latency_ms for r in rows]
    groq_lat = [r.groq_latency_ms for r in rows]
    fm_p95 = _percentile(fm_lat, 95)
    groq_p95 = _percentile(groq_lat, 95)

    fm_accuracy = sum(1 for r in rows if r.fm_correct) / n
    groq_accuracy = sum(1 for r in rows if r.groq_correct) / n
    agreement = sum(1 for r in rows if r.agree) / n

    # 同 fm_vs_groq_harness.py 的判讀準則：FM 比 Groq 慢就直接 reject。
    if fm_p95 > groq_p95:
        verdict = "reject"
    elif fm_accuracy >= 0.85:
        verdict = "switch"
    elif fm_accuracy >= 0.70:
        verdict = "borderline"
    else:
        verdict = "reject"

    return {
        "n": n,
        "fm_accuracy": fm_accuracy,
        "groq_accuracy": groq_accuracy,
        "agreement": agreement,
        "fm_latency_p50_ms": _percentile(fm_lat, 50),
        "fm_latency_p95_ms": fm_p95,
        "fm_latency_mean_ms": int(statistics.mean(fm_lat)) if fm_lat else 0,
        "groq_latency_p50_ms": _percentile(groq_lat, 50),
        "groq_latency_p95_ms": groq_p95,
        "groq_latency_mean_ms": int(statistics.mean(groq_lat)) if groq_lat else 0,
        "verdict": verdict,
    }


async def call_groq_mood(client, user_prompt: str) -> tuple[Optional[str], int]:
    start = time.monotonic()
    try:
        response = await client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": MOOD_CLASSIFIER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=200,
            reasoning_effort="low",
        )
        dt = int((time.monotonic() - start) * 1000)
        content = response.choices[0].message.content or ""
        return parse_mood_label(content), dt
    except Exception as e:
        dt = int((time.monotonic() - start) * 1000)
        logger.warning(f"Groq call failed: {e}")
        return None, dt


def call_fm_mood(fm: FMDaemon, user_prompt: str) -> tuple[Optional[str], int]:
    """FM daemon 回傳的是 stt_cleaner schema JSON；mood 任務改直接吃 raw text。

    fm_clean.swift 是通用 system/user daemon（見 fm_vs_groq_harness.FMDaemon.call），
    不綁 stt_cleaner schema，只是原本呼叫方一律 parse_cleaner_response。這裡改用
    plain-text 解析，直接從 daemon 底層 protocol 呼叫。
    """
    assert fm.proc is not None
    payload = json.dumps({"system": MOOD_CLASSIFIER_SYSTEM, "user": user_prompt}, ensure_ascii=False)
    fm.proc.stdin.write(payload + "\n")
    fm.proc.stdin.flush()
    line = fm.proc.stdout.readline()
    if not line:
        return None, 0
    try:
        resp = json.loads(line)
    except json.JSONDecodeError:
        return None, 0
    latency = int(resp.get("latency_ms", 0))
    if not resp.get("ok"):
        return None, latency
    return parse_mood_label(resp.get("content", "")), latency


def _load_corpus(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
    return rows


async def run_harness(corpus_path: Path, output_dir: Path) -> dict:
    from groq import AsyncGroq
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not set")
    groq_client = AsyncGroq(api_key=groq_key)

    fm = FMDaemon()
    print(f"[harness] starting FM daemon...", flush=True)
    fm.start()
    print("[harness] FM ready", flush=True)

    corpus = _load_corpus(corpus_path)
    print(f"[harness] {len(corpus)} samples", flush=True)

    rows: list[MoodRow] = []
    try:
        for i, sample in enumerate(corpus, 1):
            raw = sample["raw"]
            tag = sample.get("tag", "")
            expected = sample.get("expected", "")
            user_prompt = "對話片段（5 分鐘窗口）：\n" + raw

            fm_mood, fm_lat = call_fm_mood(fm, user_prompt)
            groq_mood, groq_lat = await call_groq_mood(groq_client, user_prompt)

            row = MoodRow(
                tag=tag, expected=expected,
                fm_mood=fm_mood, groq_mood=groq_mood,
                fm_latency_ms=fm_lat, groq_latency_ms=groq_lat,
                fm_correct=(fm_mood == expected),
                groq_correct=(groq_mood == expected),
                agree=(fm_mood == groq_mood),
            )
            rows.append(row)

            marker = "✓" if row.fm_correct else "✗"
            print(f"  [{i:2d}/{len(corpus)}] {marker} tag={tag} expected={expected} "
                  f"fm={fm_mood}({fm_lat}ms) groq={groq_mood}({groq_lat}ms)", flush=True)
    finally:
        fm.stop()

    report = aggregate_report(rows)
    ts = time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"fm_vs_groq_mood_report_{ts}.json"
    json_path.write_text(json.dumps({
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
