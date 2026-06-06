"""已實作的 intent_type 不該再被 analyze_agent_gaps 標 ready_to_implement
（2026-06-07：game_knowledge agent 已建+部署，但 gap 追蹤器沒「已實作」概念，
每天誤報 ready，換 agent 後可能誤導新 agent 重做）。

機制：analyze(rows, resolved) 收一組已實作 intent_type，標 resolved=True、
強制 ready_to_implement=False、不算進 ready_count；但仍保留在 intents（不默默消失，
萬一回歸還看得到 distinct 在漲）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import analyze_agent_gaps as aga


def _rows(intent: str, n: int):
    # n 筆 distinct (speaker, raw_query)
    return [{"intent_type": intent, "speaker": f"p{i}", "raw_query": f"q{i}"} for i in range(n)]


def test_unresolved_intent_still_ready():
    res = aga.analyze(_rows("game_knowledge_query", 3), resolved=set())
    it = next(i for i in res["intents"] if i["intent_type"] == "game_knowledge_query")
    assert it["ready_to_implement"] is True
    assert it.get("resolved") is False
    assert res["ready_count"] == 1


def test_resolved_intent_not_ready_but_visible():
    res = aga.analyze(_rows("game_knowledge_query", 3), resolved={"game_knowledge_query"})
    it = next(i for i in res["intents"] if i["intent_type"] == "game_knowledge_query")
    assert it["resolved"] is True
    assert it["ready_to_implement"] is False          # 已實作 → 不再 ready
    assert it["distinct_count"] == 3                   # 仍可見（不默默消失）
    assert res["ready_count"] == 0                     # 不算進 ready


def test_resolved_default_none_behaves_as_empty():
    # 向後相容：不傳 resolved 時行為同舊版（全部照門檻判）
    res = aga.analyze(_rows("some_new_intent", 2))
    it = next(i for i in res["intents"] if i["intent_type"] == "some_new_intent")
    assert it["ready_to_implement"] is True
    assert it.get("resolved") is False
