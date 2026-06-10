"""Agent gaps 離線分析 — Plan 4 Intent Gap 的 daily ritual 計數工具。

讀 records/agent_gaps.jsonl，按 **distinct (speaker, raw_query)** 算 occurrence，
排除 UNKNOWN，distinct_count ≥ 2 標 ready_to_implement。

為什麼 dedup 是核心（2026-05-30 教訓）：
同一句重複 N 次（QA 連發 / 結巴 / 跳針）若用 raw line count 會灌爆門檻
（buy_milk/replay_user_history 各 7 筆全同句，假觸發 ≥5）。distinct 計數讓
「累計 2 次」回到原意 = 兩個不同 occurrence，不是同句 2 次。

threshold=2：feedback_intent_gap_threshold.md，使用者拍板激進補 agent。

用法：python scripts/analyze_agent_gaps.py
輸出 JSON 到 stdout（與 analyze_judge_outcomes / analyze_rescue_outcomes 對齊）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

INPUT = Path("records/agent_gaps.jsonl")
RESOLVED = Path("agent_gaps_resolved.json")  # 已實作的 intent_type（dict keyed by intent_type；tracked in git，與 code 同步）
READY_THRESHOLD = 2  # distinct occurrence 門檻


def load_resolved(path: Path = RESOLVED) -> set[str]:
    """已實作 intent_type 集合。檔不存在＝空集（向後相容）。"""
    if not path.exists():
        return set()
    import json as _json
    data = _json.loads(path.read_text(encoding="utf-8"))
    return set(data.keys()) if isinstance(data, dict) else set(data)


def load(path: Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def analyze(rows: list[dict], resolved: set[str] | None = None) -> dict:
    resolved = resolved or set()
    total = len(rows)
    non_unknown = [r for r in rows if (r.get("intent_type") or "UNKNOWN") != "UNKNOWN"]

    by_type: dict[str, dict] = {}
    for r in non_unknown:
        it = r["intent_type"]
        bucket = by_type.setdefault(it, {"raw_count": 0, "distinct": set(), "samples": []})
        bucket["raw_count"] += 1
        bucket["distinct"].add((r.get("speaker", ""), r.get("raw_query", "")))
        raw = r.get("raw_query", "")
        if raw and raw not in bucket["samples"]:
            bucket["samples"].append(raw)

    intents = []
    for it, b in by_type.items():
        distinct_count = len(b["distinct"])
        is_resolved = it in resolved
        intents.append({
            "intent_type": it,
            "raw_count": b["raw_count"],
            "distinct_count": distinct_count,
            # 已實作的 intent_type 永不再 ready（但仍保留可見，回歸時看得到 distinct 漲）
            "ready_to_implement": distinct_count >= READY_THRESHOLD and not is_resolved,
            "resolved": is_resolved,
            "samples": b["samples"][:5],
        })
    intents.sort(key=lambda x: (x["distinct_count"], x["raw_count"]), reverse=True)

    return {
        "total": total,
        "total_non_unknown": len(non_unknown),
        "intents": intents,
        "ready_count": sum(1 for i in intents if i["ready_to_implement"]),
    }


async def run_clustering(gaps: list[dict], router) -> list[dict]:
    """呼叫 LLM 對 gaps 進行語意分群，回傳分群列表。"""
    if not gaps:
        return []
    
    # 抽出 raw_query 與 intent_type 簡化 prompt token
    items = []
    for r in gaps:
        items.append({
            "intent_type": r.get("intent_type", "UNKNOWN"),
            "raw_query": r.get("raw_query", "")
        })
        
    system_prompt = (
        "你是 Discord 語音 bot 的 intent gap clustering 專家。\n"
        "我將給你一組無 agent 命中的意圖（intent_type 與 raw_queries）。\n"
        "請將語意相似的意圖分群 (clustering)。\n\n"
        "對於每個分群，請決定一個最能代表該群組的 snake_case cluster_id。\n"
        "cluster_id 必須描述實際需求，且不可直接用現有 available_agents (如 music/playback_control 等)。\n\n"
        "請輸出 JSON 陣列，其 schema 如下：\n"
        "[\n"
        "  {\n"
        "    \"cluster_id\": \"buy_milk\",\n"
        "    \"members\": [\"buy_milk\", \"purchase_milk\"],\n"
        "    \"occurrence_count\": 5\n"
        "  }\n"
        "]\n"
        "請只輸出 JSON，不要有任何 Markdown 或額外文字。"
    )
    
    user_prompt = f"請對以下 gaps 進行分群：\n{json.dumps(items, ensure_ascii=False, indent=2)}"
    
    try:
        response = await router.analyze(
            prompt=user_prompt,
            caller="gap_clustering",
            system=system_prompt,
            max_tokens=1000,
            temperature=0.0,
            json=True,
        )
        if not response:
            return []
        clusters = json.loads(response)
        if isinstance(clusters, list):
            return clusters
    except Exception as e:
        print(f"[Gap Clustering] ⚠ LLM clustering 失敗: {e}", file=sys.stderr)
    return []


def save_clusters(clusters: list[dict], resolved: set[str], output_path: Path):
    """保存 clusters 至 JSON，加入 status 與過濾已實作 intent。"""
    final_clusters = []
    for c in clusters:
        cid = c.get("cluster_id")
        if not cid:
            continue
        # 排除已 resolve 的意圖
        if cid in resolved or any(m in resolved for m in c.get("members", [])):
            continue
            
        count = c.get("occurrence_count", len(c.get("members", [])))
        status = "ready_to_implement" if count >= READY_THRESHOLD else "monitoring"
        
        final_clusters.append({
            "cluster_id": cid,
            "members": c.get("members", []),
            "occurrence_count": count,
            "status": status,
        })
        
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(final_clusters, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Gap Clustering] 💾 Clustering 結果已寫入 {output_path}", file=sys.stderr)


def main() -> int:
    if not INPUT.exists():
        print(f"input not found: {INPUT}", file=sys.stderr)
        return 1
    
    gaps = load(INPUT)
    resolved = load_resolved(RESOLVED)
    result = analyze(gaps, resolved=resolved)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # Plan 4: 當 non-UNKNOWN 筆數 >= 5 筆時，自動觸發 LLM clustering
    non_unknown = [r for r in gaps if (r.get("intent_type") or "UNKNOWN") != "UNKNOWN"]
    if len(non_unknown) >= 5:
        print(f"[Gap Clustering] 🚀 non-UNKNOWN={len(non_unknown)} >= 5，開始 LLM 聚類...", file=sys.stderr)
        import os
        from dotenv import load_dotenv
        
        ROOT = Path(__file__).resolve().parent.parent
        load_dotenv(ROOT / ".env")
        
        # 呼叫 tiered router
        sys.path.insert(0, str(ROOT))
        from llm_pool import build_tiered_router
        router = build_tiered_router()
        
        import asyncio
        clusters = asyncio.run(run_clustering(non_unknown, router))
        save_clusters(clusters, resolved, Path("records/intent_clusters.json"))
        
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
