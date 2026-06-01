"""離線 replay：用歷史語料量 gap-research 的 pre-gate 通過率與 LLM 命中率。

串接 live voice_controller 前的去風險關卡——完全離線、預設零成本。
- 預設：對 stt_corrections.jsonl 的真實 utterance 跑 pre-gate，量「會放多少句去燒 LLM」。
- --with-llm：對 golden buffer 取樣跑 cheap LLM detector，量「升級後多少回 query vs NONE」
  （會耗 Groq quota，故 opt-in）。

用法：
  python scripts/replay_gap_research.py                 # 免費 pre-gate pass
  python scripts/replay_gap_research.py --with-llm -n 30 # 加量 LLM 命中率（耗 quota）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gap_research import ResearchRequest, UncertaintyDetector, has_uncertainty_signal  # noqa: E402

STT_CORRECTIONS = Path("records/stt_corrections.jsonl")
GOLDEN = Path("records/suki_golden_dataset.jsonl")


# ── 純聚合 ────────────────────────────────────────────────────────────────────

def pregate_stats(utterances: list[str]) -> dict:
    total = len(utterances)
    passed = sum(1 for u in utterances if has_uncertainty_signal(u))
    return {
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }


def summarize_detections(results: list[ResearchRequest | None]) -> dict:
    total = len(results)
    hits = sum(1 for r in results if r is not None)
    return {
        "evaluated": total,
        "gap_hits": hits,
        "hit_rate": round(hits / total, 4) if total else 0.0,
        "sample_queries": [r.query for r in results if r][:10],
    }


# ── loaders（IO）──────────────────────────────────────────────────────────────

def load_utterances_from_stt_corrections(path: Path = STT_CORRECTIONS) -> list[str]:
    """讀 raw 欄位（真實單句 utterance）。"""
    out: list[str] = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line).get("raw", "")
            except json.JSONDecodeError:
                continue
            if isinstance(raw, str) and raw:
                out.append(raw)
    return out


def load_buffers_from_golden(path: Path = GOLDEN) -> list[str]:
    """讀 user-role 內容（含『最近一分鐘現場原文』= 真實多輪對話 buffer）。"""
    out: list[str] = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs = json.loads(line).get("messages", [])
            except json.JSONDecodeError:
                continue
            user = next((m.get("content") for m in msgs if m.get("role") == "user"), None)
            if isinstance(user, str) and user:
                out.append(user)
    return out


# ── 可選 LLM pass ─────────────────────────────────────────────────────────────

async def _run_detector(buffers: list[str], detector: UncertaintyDetector) -> list:
    results = []
    for b in buffers:
        try:
            results.append(await detector.detect(b))
        except Exception:
            results.append(None)
    return results


def _build_groq_detector() -> UncertaintyDetector:
    from groq import AsyncGroq
    client = AsyncGroq()

    async def llm(prompt: str) -> str:
        resp = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=60,
        )
        return resp.choices[0].message.content.strip()

    return UncertaintyDetector(llm=llm)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-llm", action="store_true", help="加跑 LLM 命中率（耗 Groq quota）")
    ap.add_argument("-n", "--sample", type=int, default=30, help="LLM pass 取樣 buffer 數")
    args = ap.parse_args()

    utterances = load_utterances_from_stt_corrections()
    report = {"pregate": pregate_stats(utterances)}

    if args.with_llm:
        buffers = load_buffers_from_golden()
        # 只取「pre-gate 會放行」的 buffer 取樣，貼近 live 真正會送 LLM 的分佈
        gated = [b for b in buffers if has_uncertainty_signal(b)][: args.sample]
        detector = _build_groq_detector()
        results = asyncio.run(_run_detector(gated, detector))
        report["llm_pass"] = summarize_detections(results)
        report["llm_pass"]["sampled_from_gated"] = len(gated)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
