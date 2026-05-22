"""每週 recall active probe（per feedback_marvin_quality_metrics Phase 4）。

「remembers and recalls」需要外部 ground truth——憑空造的答案沒意義。所以這支只是
**框架**：讀 recall_probe_cases.json（你填的已知事實），對每個 case 查 Marvin 記憶、
比對 expect_any、record_metric("recall", correct=...)，算正確率。每週 cron 跑一次當回歸
benchmark（抓「某次改動讓記憶退步」）。

Phase 4 範圍：確定性記憶查核（suki_memory player 偏好），不經 LLM。對話端到端 recall
（RecallHandler 路徑 C，含 LLM）留 Phase 4.5。

case 格式（recall_probe_cases.json）:
  {"speaker": "大肚", "field": "likes", "expect_any": ["周杰倫"], "note": "說明"}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from quality_metrics import record_metric, summarize_recall  # noqa: E402

DEFAULT_CASES = Path("recall_probe_cases.json")


def evaluate_case(case: dict, memory) -> bool:
    """查 memory 的 speaker.field，看是否含 expect_any 任一關鍵詞。

    has_player gate：不存在的 speaker 直接 miss（不呼叫 get_player_memory 觸發建立副作用）。
    """
    speaker = case.get("speaker", "")
    field = case.get("field", "likes")
    expect = case.get("expect_any", [])
    if not speaker or not memory.has_player(speaker):
        return False
    vals = memory.get_player_memory(speaker).get(field, []) or []
    joined = " ".join(str(v) for v in vals)
    return any(kw in joined for kw in expect)


def run_probe(cases: list[dict], memory, *, record: bool = True) -> dict:
    """跑全部 case → 回 {total, correct, accuracy, results}。record=False 供測試免寫檔。"""
    results = []
    for c in cases:
        ok = evaluate_case(c, memory)
        if record:
            record_metric("recall", correct=ok, speaker=c.get("speaker", ""),
                          field=c.get("field", "likes"), note=c.get("note", ""))
        results.append({"speaker": c.get("speaker", ""), "note": c.get("note", ""), "correct": ok})
    summ = summarize_recall([{"metric": "recall", "correct": r["correct"]} for r in results])
    summ["results"] = results
    return summ


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    args = ap.parse_args()

    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"⚠️ 無 cases 檔 {cases_path} — 請填真實 ground truth 後再跑（recall benchmark 需要已知答案）")
        return
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = [c for c in cases if not c.get("_comment")]   # 跳過註解行
    if not cases:
        print("⚠️ cases 檔為空（只有註解）— recall benchmark 待你填真實事實")
        return

    from suki_memory import MemoryManager
    memory = MemoryManager()
    summ = run_probe(cases, memory)
    print(f"🧠 Recall probe: {summ['correct']}/{summ['total']} = {summ['accuracy'] * 100:.1f}%")
    for r in summ["results"]:
        mark = "✅" if r["correct"] else "❌"
        print(f"  {mark} {r['speaker']} — {r['note']}")


if __name__ == "__main__":
    main()
