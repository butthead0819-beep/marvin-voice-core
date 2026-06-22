"""pipeline_timing durable jsonl 落盤 + 防污染 guard。

2026-06-22：STAGE_TIMING 只印 log（重啟即沖、無法做延遲分布分析）。emit() 改為
同時 append 結構化 jsonl，讓 queue_wait/cleaner 段延遲跨重啟累積。寫檔沿用
judge_outcomes 同套 PYTEST_CURRENT_TEST guard，避免測試污染 prod 延遲遙測。
"""
import os

import pipeline_timing


def _fake_timing():
    # endpoint=100.0；各 stage 為 monotonic 秒值，delta 換算成 ms
    return {
        "endpoint": 100.0,
        "stt_start": 100.0,
        "stt_done": 100.2,        # +200ms
        "dequeued": 106.0,        # +6000ms  → queue_wait = 5800ms
        "cleaner_done": 108.5,    # +8500ms
        "intent_dispatched": 108.6,  # +8600ms = total
    }


def test_build_timing_row_computes_stages_and_total():
    row = pipeline_timing.build_timing_row(
        _fake_timing(), "狗與露", "馬文播放稻香", suffix=" route=main_bus"
    )
    assert row["speaker"] == "狗與露"
    assert row["route"] == "main_bus"
    assert row["stages"]["stt_done"] == 200.0
    assert row["stages"]["dequeued"] == 6000.0
    assert row["total_ms"] == 8600.0
    # queue_wait 可由分析端從絕對值算出
    assert row["stages"]["dequeued"] - row["stages"]["stt_done"] == 5800.0


def test_build_timing_row_none_without_endpoint():
    assert pipeline_timing.build_timing_row(None, "x", "y") is None
    assert pipeline_timing.build_timing_row({}, "x", "y") is None


def test_append_jsonl_is_noop_under_pytest(tmp_path, monkeypatch):
    """pytest 下不得寫入 prod 延遲遙測。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "dummy::test")
    monkeypatch.chdir(tmp_path)  # 萬一 guard 失效也只碰 tmp

    pipeline_timing._append_timing_jsonl({"ts": 1.0, "speaker": "Alice"})

    assert not (tmp_path / "records" / "pipeline_timing.jsonl").exists()
