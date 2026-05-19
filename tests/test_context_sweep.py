"""Tests for context_sweep_harness.py pure logic.

範圍：
  - trim_context: 從 prior_context 切前 N 條（最近 N 條）
  - is_wake_injection: 判斷 cleaner 是否注入 raw 沒有的喚醒詞 → 過矯正幻覺
  - aggregate_sweep_results: 多 N 的統計匯總
"""
from __future__ import annotations

import pytest

from scripts.context_sweep_harness import (
    SweepRow,
    SweepRunResult,
    aggregate_sweep_results,
    is_wake_injection_hallucination,
    trim_context,
)


# ── trim_context ──────────────────────────────────────────────────────────────

def test_trim_context_takes_last_n():
    ctx = [
        {"speaker": "a", "text": "1"},
        {"speaker": "b", "text": "2"},
        {"speaker": "c", "text": "3"},
        {"speaker": "d", "text": "4"},
    ]
    out = trim_context(ctx, n=2)
    assert out == [{"speaker": "c", "text": "3"}, {"speaker": "d", "text": "4"}]


def test_trim_context_zero_returns_empty():
    ctx = [{"speaker": "a", "text": "x"}]
    assert trim_context(ctx, n=0) == []


def test_trim_context_n_larger_than_list():
    ctx = [{"speaker": "a", "text": "x"}]
    assert trim_context(ctx, n=10) == ctx


# ── is_wake_injection_hallucination ───────────────────────────────────────────

def test_wake_injection_when_raw_no_wake_but_cleaned_has():
    # cleaner 把「今天天氣不錯」（無喚醒詞）改成「馬文，今天天氣不錯」
    assert is_wake_injection_hallucination(raw="今天天氣不錯", cleaned="馬文，今天天氣不錯") is True


def test_no_injection_when_both_have_wake():
    # raw 本來就有「馬文」，cleaner 保留 → 不是 injection
    assert is_wake_injection_hallucination(raw="馬文你好", cleaned="馬文，你好") is False


def test_no_injection_when_phonetic_typo_to_wake():
    # raw 有音近詞「麻文」→ cleaner 改成「馬文」是正常修正，不是 injection
    assert is_wake_injection_hallucination(raw="麻文播放", cleaned="馬文，播放") is False


def test_no_injection_when_both_no_wake():
    assert is_wake_injection_hallucination(raw="嗨大家好", cleaned="嗨，大家好") is False


def test_injection_with_unrelated_raw():
    # 完全沒喚醒/音近詞，cleaner 卻硬插「馬文」
    assert is_wake_injection_hallucination(raw="我去吃飯", cleaned="馬文我去吃飯") is True


# ── aggregate_sweep_results ───────────────────────────────────────────────────

def _row(n_ctx, raw="x", cleaned="x", tokens=100, is_wake=False, injection=False, wake_intent=0.0):
    return SweepRow(
        n_context=n_ctx, raw=raw, cleaned=cleaned, tokens=tokens,
        wake_intent=wake_intent, is_wake=is_wake, injection=injection,
        latency_ms=200,
    )


def test_aggregate_groups_by_n():
    rows = [
        _row(0, raw="a", cleaned="a", tokens=50),
        _row(0, raw="b", cleaned="b", tokens=60),
        _row(5, raw="a", cleaned="馬文a", tokens=150, injection=True),
        _row(5, raw="b", cleaned="b", tokens=160),
    ]
    out = aggregate_sweep_results(rows)
    assert set(out["by_n"].keys()) == {0, 5}
    assert out["by_n"][0]["n_samples"] == 2
    assert out["by_n"][5]["n_samples"] == 2
    assert out["by_n"][0]["mean_tokens"] == 55
    assert out["by_n"][5]["mean_tokens"] == 155
    assert out["by_n"][0]["injection_rate"] == 0.0
    assert out["by_n"][5]["injection_rate"] == 0.5


def test_aggregate_wake_flip_rate_vs_baseline():
    # baseline = N=5；測 N=0 跟 N=5 不一致的比率
    rows = [
        # event "a": N=5 wake, N=0 wake → no flip
        _row(5, raw="a", is_wake=True),
        _row(0, raw="a", is_wake=True),
        # event "b": N=5 wake, N=0 no wake → flip
        _row(5, raw="b", is_wake=True),
        _row(0, raw="b", is_wake=False),
        # event "c": N=5 no wake, N=0 no wake → no flip
        _row(5, raw="c", is_wake=False),
        _row(0, raw="c", is_wake=False),
    ]
    out = aggregate_sweep_results(rows, baseline_n=5)
    # 比 baseline_n=5；wake_flip_vs_baseline 應該存在於每個非 baseline n
    assert "wake_flip_vs_baseline" in out["by_n"][0]
    assert out["by_n"][0]["wake_flip_vs_baseline"] == pytest.approx(1 / 3, abs=0.01)
    # baseline 自己沒 flip 欄位 (跟自己比沒意義)
    assert out["by_n"][5].get("wake_flip_vs_baseline") in (None, 0.0)


def test_aggregate_handles_empty():
    out = aggregate_sweep_results([])
    assert out == {"by_n": {}}


# ── SweepRunResult container ──────────────────────────────────────────────────

def test_sweep_run_result_default():
    r = SweepRunResult(n=10)
    assert r.n == 10
    assert r.rows == []
