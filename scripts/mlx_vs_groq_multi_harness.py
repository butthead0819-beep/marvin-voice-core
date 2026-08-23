"""MLX 3B vs Groq — chat_classifier 與 feedback_analyzer 兩個高可行性候選 harness.

一次載入 MLX 模型（避免重複耗時 load），跑兩個任務各自的語料，各自跟 Groq
（intent judge 走 gpt-oss-20b quick tier；feedback analyzer 走同模型 analyze tier）
比對 latency + accuracy。

small_llm_judge 沒有 prod 綁定的固定 prompt（只有測試 DI fake），無法測，跳過。

使用：
    python scripts/mlx_vs_groq_multi_harness.py
    GROQ_API_KEY 必須在環境變數。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from intent_judges.groq_chat_classifier_adapter import _SYSTEM_PROMPT as CHAT_SYS  # noqa: E402
from intent_agents.feedback_analyzer import _MUSIC_SYS_PROMPT as FEEDBACK_SYS_V1, _parse_llm_response  # noqa: E402

# 實驗性 v2：補 few-shot 區分 skipped_immediately vs negative（2B 模型對「推薦後幾秒內」
# 這種隱含時間推理弱，光靠規則 5「立刻」兩個字撐不住，加具體秒數範例試試看能不能撐起來）。
FEEDBACK_SYS_V2 = FEEDBACK_SYS_V1 + (
    "\n\n範例（注意 timestamp 差異）：\n"
    "- \"+1.0s: 不要這首，換掉\" → skipped_immediately（<1.5秒內明確拒絕/换掉，屬於立即跳過）\n"
    "- \"+0.8s: 跳過跳過\" → skipped_immediately（<1.5秒內要求跳過）\n"
    "- \"+2.5s: 這首不行換掉\" → negative（≥2秒後才反應，屬於一般負評非立即跳過）\n"
    "- \"+3.0s: 不喜歡這首欸\" → negative（≥2秒後的評論）\n"
    "判斷 skipped_immediately 的關鍵是 timestamp < 1.5秒，不是只看字面有沒有「換」「跳過」。"
)
FEEDBACK_SYS = FEEDBACK_SYS_V2 if os.environ.get("FEEDBACK_PROMPT_V2") else FEEDBACK_SYS_V1

MODEL_ID = os.environ.get("MLX_MODEL", "mlx-community/Qwen2.5-3B-Instruct-4bit")

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)
_FENCE_END_RE = re.compile(r"\n?```\s*.*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    s = _FENCE_RE.sub("", s, count=1)
    s = _FENCE_END_RE.sub("", s, count=1)
    return s.strip()


@dataclass
class Row:
    tag: str
    mlx_latency_ms: int
    groq_latency_ms: int
    mlx_correct: bool
    groq_correct: bool
    mlx_raw: str
    groq_raw: str


def _percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def _summarize(name: str, rows: list[Row]) -> dict:
    n = len(rows)
    mlx_lat = [r.mlx_latency_ms for r in rows]
    groq_lat = [r.groq_latency_ms for r in rows]
    return {
        "task": name,
        "n": n,
        "mlx_accuracy": sum(r.mlx_correct for r in rows) / n,
        "groq_accuracy": sum(r.groq_correct for r in rows) / n,
        "mlx_latency_p50_ms": _percentile(mlx_lat, 50),
        "mlx_latency_p95_ms": _percentile(mlx_lat, 95),
        "mlx_latency_mean_ms": int(sum(mlx_lat) / n),
        "groq_latency_p50_ms": _percentile(groq_lat, 50),
        "groq_latency_p95_ms": _percentile(groq_lat, 95),
        "groq_latency_mean_ms": int(sum(groq_lat) / n),
    }


# ── MLX / Groq generic callers ──────────────────────────────────────────────

def mlx_call(model, tokenizer, system: str, user: str, max_tokens: int) -> tuple[str, int]:
    from mlx_lm import generate
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    except Exception:
        # 部分 chat template（如 Gemma-2）不支援獨立 system role，併入 user turn。
        merged = [{"role": "user", "content": f"{system}\n\n{user}"}]
        prompt = tokenizer.apply_chat_template(merged, add_generation_prompt=True, tokenize=False)
    start = time.monotonic()
    text = generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False)
    dt = int((time.monotonic() - start) * 1000)
    return text, dt


async def groq_call(client, system: str, user: str, max_tokens: int, json_mode: bool) -> tuple[str, int]:
    start = time.monotonic()
    try:
        kwargs = dict(
            model="openai/gpt-oss-20b",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=max_tokens,
            reasoning_effort="low",
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = await client.chat.completions.create(**kwargs)
        dt = int((time.monotonic() - start) * 1000)
        return response.choices[0].message.content or "", dt
    except Exception as e:
        dt = int((time.monotonic() - start) * 1000)
        return f"__ERROR__:{e}", dt


def _load_corpus(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
    return rows


# ── Task 1: chat_classifier ─────────────────────────────────────────────────

async def run_chat_classifier(model, tokenizer, groq_client, output_dir: Path) -> dict:
    corpus = _load_corpus(REPO_ROOT / "tests/fixtures/chat_classifier_harness_corpus.jsonl")
    rows: list[Row] = []
    for i, s in enumerate(corpus, 1):
        user_msg = f'raw="{s["raw"]}" intent="{s["intent"]}"'
        mlx_raw, mlx_lat = mlx_call(model, tokenizer, CHAT_SYS, user_msg, max_tokens=80)
        groq_raw, groq_lat = await groq_call(groq_client, CHAT_SYS, user_msg, max_tokens=150, json_mode=True)

        def _is_chat(text: str) -> Optional[bool]:
            try:
                data = json.loads(_strip_fences(text))
                return bool(data.get("is_chat"))
            except Exception:
                return None

        mlx_v, groq_v = _is_chat(mlx_raw), _is_chat(groq_raw)
        row = Row(
            tag=s["tag"], mlx_latency_ms=mlx_lat, groq_latency_ms=groq_lat,
            mlx_correct=(mlx_v == s["expected_is_chat"]),
            groq_correct=(groq_v == s["expected_is_chat"]),
            mlx_raw=mlx_raw[:120], groq_raw=groq_raw[:120],
        )
        rows.append(row)
        m = "✓" if row.mlx_correct else "✗"
        print(f"  chat_classifier [{i:2d}/{len(corpus)}] {m} {s['tag']} "
              f"mlx={mlx_v}({mlx_lat}ms) groq={groq_v}({groq_lat}ms)", flush=True)

    summary = _summarize("chat_classifier", rows)
    _write(output_dir, "chat_classifier", summary, rows)
    return summary


# ── Task 2: feedback_analyzer (music) ───────────────────────────────────────

def _build_feedback_user_msg(s: dict) -> str:
    lines = [
        f"speaker: {s['speaker']}",
        f"recommended: {s['selected']}",
        f"explanation_uttered: {s['explanation_uttered']}",
        f"trigger: {s['trigger']}",
        "",
        "speaker's utterances in feedback window:",
    ]
    for u in s["utts"]:
        lines.append(f"  +{u['delta']:.1f}s: {u['text']}")
    return "\n".join(lines)


async def run_feedback_analyzer(model, tokenizer, groq_client, output_dir: Path) -> dict:
    corpus = _load_corpus(REPO_ROOT / "tests/fixtures/feedback_analyzer_harness_corpus.jsonl")
    rows: list[Row] = []
    for i, s in enumerate(corpus, 1):
        user_msg = _build_feedback_user_msg(s)
        mlx_raw, mlx_lat = mlx_call(model, tokenizer, FEEDBACK_SYS, user_msg, max_tokens=300)
        groq_raw, groq_lat = await groq_call(groq_client, FEEDBACK_SYS, user_msg, max_tokens=400, json_mode=True)

        mlx_parsed = _parse_llm_response(_strip_fences(mlx_raw))
        groq_parsed = _parse_llm_response(_strip_fences(groq_raw))
        mlx_sent = mlx_parsed.sentiment if mlx_parsed else None
        groq_sent = groq_parsed.sentiment if groq_parsed else None

        row = Row(
            tag=s["tag"], mlx_latency_ms=mlx_lat, groq_latency_ms=groq_lat,
            mlx_correct=(mlx_sent == s["expected"]),
            groq_correct=(groq_sent == s["expected"]),
            mlx_raw=mlx_raw[:150], groq_raw=groq_raw[:150],
        )
        rows.append(row)
        m = "✓" if row.mlx_correct else "✗"
        print(f"  feedback_analyzer [{i:2d}/{len(corpus)}] {m} {s['tag']} "
              f"mlx={mlx_sent}({mlx_lat}ms) groq={groq_sent}({groq_lat}ms)", flush=True)

    summary = _summarize("feedback_analyzer", rows)
    _write(output_dir, "feedback_analyzer", summary, rows)
    return summary


def _write(output_dir: Path, name: str, summary: dict, rows: list[Row]) -> None:
    ts = time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"mlx_vs_groq_{name}_report_{ts}.json"
    path.write_text(json.dumps({
        "model": MODEL_ID,
        "summary": summary,
        "samples": [asdict(r) for r in rows],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[harness] {name} report written: {path}")


async def main_async() -> None:
    from groq import AsyncGroq
    from mlx_lm import load

    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not set")
    groq_client = AsyncGroq(api_key=groq_key)

    print(f"[harness] loading MLX model {MODEL_ID}...", flush=True)
    t0 = time.monotonic()
    model, tokenizer = load(MODEL_ID)
    print(f"[harness] MLX model loaded in {time.monotonic() - t0:.1f}s", flush=True)

    output_dir = REPO_ROOT / "records"
    tasks = os.environ.get("HARNESS_TASKS", "chat_classifier,feedback_analyzer").split(",")
    results = {}
    if "chat_classifier" in tasks:
        results["chat_classifier"] = await run_chat_classifier(model, tokenizer, groq_client, output_dir)
    if "feedback_analyzer" in tasks:
        results["feedback_analyzer"] = await run_feedback_analyzer(model, tokenizer, groq_client, output_dir)

    print("\n=== SUMMARY ===")
    print(json.dumps(results, ensure_ascii=False, indent=2))


def main() -> int:
    asyncio.run(main_async())
    return 0


if __name__ == "__main__":
    sys.exit(main())
