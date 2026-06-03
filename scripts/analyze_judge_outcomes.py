"""Judge outcomes 離線分析 — shadow race 數據量化。

讀 records/judge_outcomes.jsonl，輸出：
  - J1 fast-path 率（winning_judge=j1_regex）+ j3_precomputed 搶贏率 + J1 被取消數
  - 語意一致率（只配對兩邊 completed；guard≡cleaner dense-zero 視為同 NO_INTENT）
  - J1 / J3 p50 / p95 latency（只算 completed）
  - winner_name (agent) histogram + winning_judge 分佈
  - guard_too_aggressive：J1 NO_INTENT 但 J3 救回真 intent（race-rule 改善依據）
  - j1_false_positive：J1 有 intent 但 J3 cleaned 後判無（J1 over-trigger）
  - j2_seen_count：含 J2 judge 的 row 數（驗 shadow J2 是否真在跑）
  - weak_play_curation 卡 threshold 的 case 數
  - 兩 judge 都 NO_INTENT 的 case 列表（noise / wake-spam）

⚠️ 重要：judge race 有 cancelled 狀態（precomputed J3 先到 → J1 被取消）與
j3_cleaner_precomputed 這條路徑。一致率必須 (a) 排除未完成的 judge、(b) 比語意
結果非 bid_name 字串，否則 cancelled J1 與 guard(0.96) 會假性「不一致」拉低數字。

用途：每天/每週跑一次，把報告貼到 records/judge_outcomes_analysis_<date>.md
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from statistics import median

INPUT = Path("records/judge_outcomes.jsonl")
J1_THRESHOLD = 0.85  # production threshold（commit 8960a67：0.90→0.85）

# 「無意圖」語意 marker：guard=J1 擋下、cleaner_judge=J3 dense-zero。
# 兩者 bid_name 不同但語意同 = NO_INTENT，比一致率時必須等價（否則 guard(0.96)
# 會假性「不一致」於 cleaner_judge(0.00)）。confidence<MIN 的任何 bid 也算 NO_INTENT。
NEG_BID_NAMES = frozenset({"guard", "cleaner_judge", None, ""})
MIN_CONFIDENCE = 0.30


def _fmt_judge(j: dict) -> str:
    """name(confidence) 安全格式化。confidence=None → '?'（不假裝成 0.00，
    那會跟真實 0 信心混淆）。cancelled 的 judge 標出來。"""
    if j.get("status") == "cancelled":
        return f"{j.get('bid_name')}(cancelled)"
    conf = j.get("confidence")
    conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
    return f"{j.get('bid_name')}({conf_str})"


def _outcome(j: dict | None) -> str | None:
    """judge 的語意結果。未完成（cancelled/error）→ None（不納入一致率配對）；
    NO_INTENT marker 或低信心 → "NO_INTENT"；否則回 actionable agent 名。"""
    if not j or j.get("status") != "completed":
        return None
    if j.get("bid_name") in NEG_BID_NAMES or (j.get("confidence") or 0) < MIN_CONFIDENCE:
        return "NO_INTENT"
    return j.get("bid_name")


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(len(values) * p))
    return values[idx]


def load() -> list[dict]:
    rows: list[dict] = []
    with INPUT.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def analyze(rows: list[dict]) -> dict:
    total = len(rows)
    if not total:
        return {"total": 0}

    j1_lat: list[float] = []   # 只收 completed（cancelled 的 latency=被取消時點，非真完成耗時）
    j3_lat: list[float] = []
    winner_names: Counter[str] = Counter()
    winning_judges: Counter[str] = Counter()

    j1_fastpath = 0       # winning_judge=j1_regex（J1 贏 race；它贏就代表過了自己的 threshold）
    precomputed_win = 0   # j3_cleaner_precomputed 搶贏（通常連帶 cancel J1）
    j1_cancelled = 0      # J1 被 race 取消（precomputed J3 先到）→ 不是 miss
    # J2 是包在 J1 外的 veto wrapper（非獨立 judge），靠 J1 bid_reason 的足跡觀測：
    #   vetoed_by_chat → J2 否決；j2_ran → J2 跑了沒否決（含 llm_timeout/exception fail-safe）
    j2_executed = 0       # J2 真的執行過（ran 或 veto）
    j2_veto = 0           # J2 否決
    j2_failsafe = 0       # J2 跑了但 fail-safe（timeout/exception/malformed）→ 靜默退化警訊
    both_dense_zero = []
    guard_with_j3_intent = []   # 議題 A：J1 guard 太兇、J3 救回真 intent
    j1_fp_j3_says_no = []       # J1 假陽性：J1 actionable、J3 判 NO_INTENT
    weak_curation_at_threshold = []  # 議題 B
    sem_agree = 0
    sem_disagree = []

    for r in rows:
        winning_judges[r.get("winning_judge") or "_none_"] += 1
        winner_names[r.get("winner_name") or "_none_"] += 1
        if r.get("winning_judge") == "j3_cleaner_precomputed":
            precomputed_win += 1

        judges = {j["name"]: j for j in r.get("judges", [])}
        j1 = judges.get("j1_regex")
        j3 = judges.get("j3_cleaner_precomputed") or judges.get("j3_cleaner")
        j1_reason = (j1.get("bid_reason") or "") if j1 else ""
        if "vetoed_by_chat" in j1_reason:
            j2_executed += 1
            j2_veto += 1
        elif "j2_ran" in j1_reason:
            j2_executed += 1
            if any(t in j1_reason for t in ("llm_timeout", "llm_exception", "malformed")):
                j2_failsafe += 1
        if j1 and j1.get("status") == "cancelled":
            j1_cancelled += 1
        if j1 and j1.get("status") == "completed":
            j1_lat.append(j1.get("latency_ms", 0))
        if j3 and j3.get("status") == "completed":
            j3_lat.append(j3.get("latency_ms", 0))

        if r.get("winning_judge") == "j1_regex":
            j1_fastpath += 1

        # 一致率：只配對「兩邊都 completed」，比語意結果（NO_INTENT 等價）
        o1, o3 = _outcome(j1), _outcome(j3)
        if o1 is not None and o3 is not None:
            if o1 == "NO_INTENT" and o3 == "NO_INTENT":
                both_dense_zero.append(r["raw_query"])
                sem_agree += 1
            elif o1 == o3:
                sem_agree += 1
            else:
                sem_disagree.append({
                    "raw": r["raw_query"],
                    "j1": _fmt_judge(j1),
                    "j3": _fmt_judge(j3),
                    "j1_outcome": o1,
                    "j3_outcome": o3,
                })
                # 議題 A：J1 NO_INTENT（多為 guard 太兇）但 J3 有真 intent
                if o1 == "NO_INTENT" and o3 != "NO_INTENT":
                    guard_with_j3_intent.append({
                        "raw": r["raw_query"],
                        "j1_reason": j1.get("bid_reason"),
                        "j3": _fmt_judge(j3),
                    })
                # J1 假陽性：J1 有 intent 但 J3 cleaned 後判無
                if o1 != "NO_INTENT" and o3 == "NO_INTENT":
                    j1_fp_j3_says_no.append({
                        "raw": r["raw_query"],
                        "j1": _fmt_judge(j1),
                        "j1_reason": j1.get("bid_reason"),
                    })

            # 議題 B：J1 curation 卡 threshold（兩 judge 語意一致）
            reason = (j1.get("bid_reason") or "") if j1 else ""
            if (
                "weak_play_curation" in reason
                and 0.84 <= (j1.get("confidence") or 0) <= 0.86
                and o1 == o3
            ):
                weak_curation_at_threshold.append(r["raw_query"])

    completed_pairs = sem_agree + len(sem_disagree)
    return {
        "total": total,
        "j1_fastpath_rate": j1_fastpath / total,
        "precomputed_win_rate": precomputed_win / total,
        "j1_cancelled_count": j1_cancelled,
        # J2 觀測（靠 J1 bid_reason 足跡；舊資料無足跡 → 0，2026-06-03 加 footprint 後才有）
        "j2_executed_count": j2_executed,   # 0 且近期有量 → 查接線；>0 才談 veto 率
        "j2_veto_count": j2_veto,
        "j2_failsafe_count": j2_failsafe,   # >0 = J2 靜默退化（timeout/exception）警訊
        "j1_p50_ms": median(j1_lat) if j1_lat else 0,
        "j1_p95_ms": percentile(j1_lat, 0.95),
        "j3_p50_ms": median(j3_lat) if j3_lat else 0,
        "j3_p95_ms": percentile(j3_lat, 0.95),
        "winning_judges": dict(winning_judges),
        "winner_agents": dict(winner_names),
        # 語意一致率：guard≡cleaner dense-zero 視為同 NO_INTENT，只算兩邊都完成的 pair
        "semantic_agree_rate": (sem_agree / completed_pairs) if completed_pairs else 0,
        "semantic_agree_count": sem_agree,
        "completed_pairs": completed_pairs,
        "semantic_disagree": sem_disagree,
        "both_no_intent_count": len(both_dense_zero),
        "both_no_intent_samples": both_dense_zero[:10],
        "guard_too_aggressive": guard_with_j3_intent,
        "j1_false_positive": j1_fp_j3_says_no,
        "weak_curation_at_threshold_count": len(weak_curation_at_threshold),
        "weak_curation_at_threshold_samples": weak_curation_at_threshold[:10],
    }


def main() -> int:
    if not INPUT.exists():
        print(f"input not found: {INPUT}", file=sys.stderr)
        return 1
    rows = load()
    result = analyze(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
