"""Apple Foundation Model vs Groq llama-3.1-8b-instant cleaner harness.

跑同一組 STT raw 文字 → 分別打 FM CLI daemon 與 Groq 8b → 對照 cleaned/wake/延遲。
產出 records/fm_vs_groq_report_<ts>.json + .md。

純函式（strip / parse / compare / aggregate）有 tests/test_fm_harness.py 覆蓋。
I/O 部分（subprocess、groq HTTP）走 integration smoke。

使用：
    python scripts/fm_vs_groq_harness.py [corpus.jsonl]
    GROQ_API_KEY 必須在環境變數。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
FM_BINARY = REPO_ROOT / "records" / "swift-fm-cli" / "fm_clean"
WAKE_THRESHOLD = 0.70  # 對齊 stt_cleaner.py 的 WAKE_THRESHOLD

logger = logging.getLogger("fm_harness")

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)
_FENCE_END_RE = re.compile(r"\n?```\s*.*$", re.DOTALL)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CleanerResult:
    cleaned: str
    intent: float
    calling: bool
    is_complete: bool

    @property
    def is_wake(self) -> bool:
        # 對齊 stt_cleaner._build_res：intent>=0.75 直 wake；0.65-0.75 看 calling；其他 not wake
        if self.intent >= 0.75:
            return True
        if self.intent >= 0.65:
            return self.calling
        return False


@dataclass
class ComparisonRow:
    raw: str
    cleaned_agree: bool
    wake_decision_agree: bool
    fm_parse_ok: bool
    groq_parse_ok: bool
    fm_latency_ms: int
    groq_latency_ms: int
    fm_cleaned: Optional[str]
    groq_cleaned: Optional[str]
    fm_intent: Optional[float]
    groq_intent: Optional[float]


# ── Pure logic (tested) ───────────────────────────────────────────────────────

def strip_json_fences(text: str) -> str:
    """剝掉 ```json ... ``` 包裹。沒包就直接回傳。"""
    s = text.strip()
    if not s.startswith("```"):
        return s
    s = _FENCE_RE.sub("", s, count=1)
    s = _FENCE_END_RE.sub("", s, count=1)
    return s.strip()


def parse_cleaner_response(text: str) -> Optional[CleanerResult]:
    """解析 cleaner JSON。失敗（包含 schema 違反）回 None。"""
    if not text:
        return None
    s = strip_json_fences(text)
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    cleaned = data.get("cleaned")
    intent = data.get("intent")
    calling = data.get("calling")
    is_complete = data.get("is_complete", True)

    # 嚴格 schema 驗證：FM 常把 intent 變字串、calling 變字串
    if not isinstance(cleaned, str) or not cleaned.strip():
        return None
    if not isinstance(intent, (int, float)) or isinstance(intent, bool):
        return None
    if not isinstance(calling, bool):
        return None
    if not isinstance(is_complete, bool):
        return None

    intent_f = max(0.0, min(1.0, float(intent)))
    return CleanerResult(cleaned=cleaned.strip(), intent=intent_f,
                         calling=calling, is_complete=is_complete)


def compare_outputs(
    *,
    raw: str,
    fm: Optional[CleanerResult],
    groq: Optional[CleanerResult],
    fm_latency_ms: int,
    groq_latency_ms: int,
) -> ComparisonRow:
    """比對 FM vs Groq 結果，分類 cleaned / wake 同意。

    cleaned_agree：兩邊都 parse 成功 + cleaned 完全相等
    wake_decision_agree：兩邊都 parse 成功 + is_wake 相等
    任一邊 parse fail → 兩個 agree 都判 False（不能說「同意」）
    """
    fm_ok = fm is not None
    groq_ok = groq is not None
    if not (fm_ok and groq_ok):
        return ComparisonRow(
            raw=raw, cleaned_agree=False, wake_decision_agree=False,
            fm_parse_ok=fm_ok, groq_parse_ok=groq_ok,
            fm_latency_ms=fm_latency_ms, groq_latency_ms=groq_latency_ms,
            fm_cleaned=fm.cleaned if fm else None,
            groq_cleaned=groq.cleaned if groq else None,
            fm_intent=fm.intent if fm else None,
            groq_intent=groq.intent if groq else None,
        )
    return ComparisonRow(
        raw=raw,
        cleaned_agree=(fm.cleaned == groq.cleaned),
        wake_decision_agree=(fm.is_wake == groq.is_wake),
        fm_parse_ok=True, groq_parse_ok=True,
        fm_latency_ms=fm_latency_ms, groq_latency_ms=groq_latency_ms,
        fm_cleaned=fm.cleaned, groq_cleaned=groq.cleaned,
        fm_intent=fm.intent, groq_intent=groq.intent,
    )


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def aggregate_report(rows: list[ComparisonRow]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "verdict": "empty"}

    fm_ok = [r for r in rows if r.fm_parse_ok]
    groq_ok = [r for r in rows if r.groq_parse_ok]
    both_ok = [r for r in rows if r.fm_parse_ok and r.groq_parse_ok]

    cleaned_agreement = (
        sum(1 for r in both_ok if r.cleaned_agree) / len(both_ok)
        if both_ok else 0.0
    )
    wake_agreement = (
        sum(1 for r in both_ok if r.wake_decision_agree) / len(both_ok)
        if both_ok else 0.0
    )

    fm_lat = [r.fm_latency_ms for r in rows]
    groq_lat = [r.groq_latency_ms for r in rows]

    fm_p95 = _percentile(fm_lat, 95)
    groq_p95 = _percentile(groq_lat, 95)

    # Verdict — 事先講好的判讀準則（記憶 Step 3）
    # FM 比 Groq 慢就直接 reject：veto-only 也沒理由用更慢的引擎
    if fm_p95 > groq_p95:
        verdict = "reject"
    elif cleaned_agreement >= 0.85 and wake_agreement >= 0.90:
        verdict = "switch"
    elif cleaned_agreement >= 0.70 and wake_agreement >= 0.90:
        verdict = "wake_veto_only"
    else:
        verdict = "reject"

    return {
        "n": n,
        "fm_parse_success_rate": len(fm_ok) / n,
        "groq_parse_success_rate": len(groq_ok) / n,
        "cleaned_agreement": cleaned_agreement,
        "wake_decision_agreement": wake_agreement,
        "fm_latency_p50_ms": _percentile(fm_lat, 50),
        "fm_latency_p95_ms": fm_p95,
        "fm_latency_mean_ms": int(statistics.mean(fm_lat)) if fm_lat else 0,
        "groq_latency_p50_ms": _percentile(groq_lat, 50),
        "groq_latency_p95_ms": groq_p95,
        "groq_latency_mean_ms": int(statistics.mean(groq_lat)) if groq_lat else 0,
        "verdict": verdict,
    }


# ── I/O glue (integration only, not unit-tested) ──────────────────────────────

class FMDaemon:
    """Wraps the Swift FM stdin daemon. One request per line."""

    def __init__(self, binary: Path = FM_BINARY):
        self.binary = binary
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if not self.binary.exists():
            raise FileNotFoundError(
                f"FM binary not built: {self.binary}\n"
                f"Run: cd {self.binary.parent} && swiftc -O fm_clean.swift -o fm_clean"
            )
        self.proc = subprocess.Popen(
            [str(self.binary)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        # Wait for "[fm_clean] ready" on stderr (model warm-up)
        line = self.proc.stderr.readline()
        if "ready" not in line:
            raise RuntimeError(f"FM daemon did not signal ready: {line!r}")

    def call(self, system: str, user: str) -> tuple[Optional[CleanerResult], int]:
        assert self.proc is not None
        payload = json.dumps({"system": system, "user": user}, ensure_ascii=False)
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            return None, 0
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            return None, 0
        latency = int(resp.get("latency_ms", 0))
        if not resp.get("ok"):
            return None, latency
        return parse_cleaner_response(resp.get("content", "")), latency

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


async def call_groq_8b(client, system: str, user: str) -> tuple[Optional[CleanerResult], int]:
    """Call Groq llama-3.1-8b-instant — same shape as stt_cleaner.py."""
    start = time.monotonic()
    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        dt = int((time.monotonic() - start) * 1000)
        raw_output = response.choices[0].message.content
        return parse_cleaner_response(raw_output), dt
    except Exception as e:
        dt = int((time.monotonic() - start) * 1000)
        logger.warning(f"Groq call failed: {e}")
        return None, dt


def _build_user_message(raw: str, context: Optional[list[dict]] = None) -> str:
    if context:
        background = "\n".join(f"{u['speaker']}：{u['text']}" for u in context)
        return f"<Background>\n{background}\n</Background>\n\n<Target>{raw}</Target>"
    return f"<Target>{raw}</Target>"


def _load_corpus(path: Path) -> list[dict]:
    """Load corpus JSONL: each line is {raw, speaker?, context?, tag?}."""
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(f"skip malformed line: {line[:60]}")
    return rows


async def run_harness(corpus_path: Path, output_dir: Path) -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    from marvin_prompts import PromptManager

    pm = PromptManager()
    system_prompt = pm.get_instruction("stt_cleaner", vision_enabled=False)

    from groq import AsyncGroq
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not set")
    groq_client = AsyncGroq(api_key=groq_key)

    fm = FMDaemon()
    print(f"[harness] starting FM daemon ({FM_BINARY})...", flush=True)
    fm.start()
    print("[harness] FM ready", flush=True)

    corpus = _load_corpus(corpus_path)
    print(f"[harness] {len(corpus)} samples", flush=True)

    rows: list[ComparisonRow] = []
    per_sample: list[dict] = []

    try:
        for i, sample in enumerate(corpus, 1):
            raw = sample["raw"]
            ctx = sample.get("context")
            tag = sample.get("tag", "")
            user_msg = _build_user_message(raw, ctx)

            # Sequential: FM first (stdin/stdout is serial anyway), then Groq.
            fm_result, fm_lat = fm.call(system_prompt, user_msg)
            groq_result, groq_lat = await call_groq_8b(groq_client, system_prompt, user_msg)

            row = compare_outputs(
                raw=raw, fm=fm_result, groq=groq_result,
                fm_latency_ms=fm_lat, groq_latency_ms=groq_lat,
            )
            rows.append(row)
            per_sample.append({**asdict(row), "tag": tag})

            agree_marker = "✓" if row.cleaned_agree else "✗"
            print(f"  [{i:2d}/{len(corpus)}] {agree_marker} tag={tag} "
                  f"fm={fm_lat}ms groq={groq_lat}ms raw='{raw[:30]}'", flush=True)
    finally:
        fm.stop()

    report = aggregate_report(rows)
    ts = time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"fm_vs_groq_report_{ts}.json"
    json_path.write_text(json.dumps({
        "summary": report,
        "samples": per_sample,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = output_dir / f"fm_vs_groq_report_{ts}.md"
    md_path.write_text(_render_markdown(report, per_sample), encoding="utf-8")

    print(f"\n[harness] report written:\n  {json_path}\n  {md_path}")
    print(f"[harness] verdict: {report['verdict']}")
    return report


def _render_markdown(report: dict, samples: list[dict]) -> str:
    lines = [
        "# FM vs Groq 8b Cleaner Report",
        "",
        f"- Samples: **{report['n']}**",
        f"- Verdict: **{report['verdict']}**",
        "",
        "## Aggregate",
        "",
        f"| Metric | FM | Groq 8b |",
        f"|---|---|---|",
        f"| Parse success | {report['fm_parse_success_rate']:.1%} | {report['groq_parse_success_rate']:.1%} |",
        f"| Latency p50 | {report['fm_latency_p50_ms']}ms | {report['groq_latency_p50_ms']}ms |",
        f"| Latency p95 | {report['fm_latency_p95_ms']}ms | {report['groq_latency_p95_ms']}ms |",
        f"| Latency mean | {report['fm_latency_mean_ms']}ms | {report['groq_latency_mean_ms']}ms |",
        "",
        f"- Cleaned agreement (both parsed): **{report['cleaned_agreement']:.1%}**",
        f"- Wake decision agreement (both parsed): **{report['wake_decision_agreement']:.1%}**",
        "",
        "## Per-sample disagreements",
        "",
    ]
    disagree = [s for s in samples if not s["cleaned_agree"] or not s["wake_decision_agree"]]
    for s in disagree:
        lines.append(f"### `{s['tag']}` raw=`{s['raw']}`")
        lines.append(f"- FM: `{s['fm_cleaned']}` intent={s['fm_intent']}")
        lines.append(f"- Groq: `{s['groq_cleaned']}` intent={s['groq_intent']}")
        lines.append(f"- cleaned_agree={s['cleaned_agree']} wake_agree={s['wake_decision_agree']} "
                     f"fm_parse={s['fm_parse_ok']} groq_parse={s['groq_parse_ok']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    corpus_arg = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/fm_harness_corpus.jsonl"
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
