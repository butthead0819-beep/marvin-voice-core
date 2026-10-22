"""scripts/analyze_pipeline_timing：E1 的 kind=llm 列不可灌高派發筆數。"""
from __future__ import annotations

from scripts.analyze_pipeline_timing import analyze


def test_llm_rows_not_counted_as_dispatches():
    rows = [
        {"ts": 1000.0, "speaker": "alice", "stages": {"stt_done": 300.0, "dequeued": 900.0}, "total_ms": 900.0},
        {"ts": 1001.0, "speaker": "alice", "kind": "llm", "route": "cloud",
         "stages": {"dequeued": 900.0, "llm_start": 1500.0}},
    ]
    out = analyze(rows, days=1, min_samples=99)
    assert "1 筆真人派發" in out
