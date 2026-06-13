"""語意斷句決策核心（Volatile Phase 1，2026-06-13 hot sprint）。

把 volatile 文字流轉成「何時提前切句」的決策，取代 VAD 純靠靜默計時的等待。
核心觀念：文字穩定 N ms（穩定窗）＝講者講完了，不必等滿 VAD 靜默 0.8-3s。

無翻盤率數據的減災（hot sprint）：
- revision（文字非延伸而是改寫）→ 重置穩定窗（模型還不確定，往後延）
- 穩定窗有下限、min 語句長度，短碎片不亂切
- 穩定窗吃對話溫度（高溫長、低溫短），沿 VAD 既有溫度語意
"""
from __future__ import annotations

from streaming_endpointer import SemanticEndpointer


def _feed(ep: SemanticEndpointer, events: list[tuple[int, str]]):
    """餵 (t_ms, text) 序列，回傳第一個 cut 決策（或 None）。"""
    for t_ms, text in events:
        decision = ep.observe(t_ms, text)
        if decision is not None:
            return decision
    return None


# ── 基本斷句 ────────────────────────────────────────────────────────────────

def test_cuts_after_text_stable_for_window():
    ep = SemanticEndpointer(stability_window_ms=500, min_duration_ms=200)
    d = _feed(ep, [
        (100, "馬文"), (200, "馬文播放"), (300, "馬文播放晴天"),
        (820, "馬文播放晴天"),  # 距上次變動 520ms ≥ 500 → 切
    ])
    assert d is not None
    assert d.text == "馬文播放晴天"
    assert d.cut_ms == 820


def test_no_cut_while_text_keeps_changing():
    ep = SemanticEndpointer(stability_window_ms=500, min_duration_ms=200)
    d = _feed(ep, [(100, "馬"), (300, "馬文"), (500, "馬文播"), (700, "馬文播放")])
    assert d is None  # 每 200ms 就變，從未穩定滿 500


def test_min_duration_blocks_premature_cut():
    """極短語句即使馬上穩定，也要過 min_duration 才切（防雜訊碎片）。"""
    ep = SemanticEndpointer(stability_window_ms=300, min_duration_ms=1000)
    d = _feed(ep, [(50, "嗯"), (400, "嗯"), (700, "嗯")])
    assert d is None  # 穩定但總長 650ms < 1000


# ── revision 保護（無翻盤數據的核心減災）──────────────────────────────────

def test_revision_resets_stability_clock():
    """文字被改寫（非延伸）→ 穩定窗重置，切點往後延。"""
    ep = SemanticEndpointer(stability_window_ms=400, min_duration_ms=100)
    d = _feed(ep, [
        (100, "馬聞"), (300, "馬聞播放"),
        (500, "馬文播放"),       # 改寫 馬聞→馬文：重置時鐘
        (820, "馬文播放"),       # 距重置 320ms < 400 → 還不切
    ])
    assert d is None


def test_revision_then_stable_cuts_later():
    ep = SemanticEndpointer(stability_window_ms=400, min_duration_ms=100)
    d = _feed(ep, [
        (100, "馬聞"), (300, "馬文播放"),  # 改寫
        (750, "馬文播放"),                  # 距改寫 450ms ≥ 400 → 切
    ])
    assert d is not None
    assert d.cut_ms == 750
    assert d.revision_count == 1


def test_pure_extension_not_treated_as_revision():
    ep = SemanticEndpointer(stability_window_ms=400, min_duration_ms=100)
    d = _feed(ep, [
        (100, "馬文"), (300, "馬文播放"), (500, "馬文播放晴天"),  # 全延伸
        (920, "馬文播放晴天"),
    ])
    assert d is not None
    assert d.revision_count == 0


# ── 對話溫度 ────────────────────────────────────────────────────────────────

def test_temperature_widens_stability_window():
    """高溫對話用長穩定窗（不急著切），低溫用短窗（快回應）——沿 VAD 溫度語意。"""
    hot = SemanticEndpointer.from_temperature("high")
    cold = SemanticEndpointer.from_temperature("low")
    assert hot._stability_window_ms > cold._stability_window_ms


def test_stability_window_has_floor():
    """穩定窗永不低於下限（無翻盤數據時的保守保護）。"""
    ep = SemanticEndpointer.from_temperature("low")
    assert ep._stability_window_ms >= 500


# ── 標點/空格正規化 ─────────────────────────────────────────────────────────

def test_punctuation_change_not_counted_as_revision():
    """只差標點/空格不算文字變動（SwiftV2 會補逗號）。"""
    ep = SemanticEndpointer(stability_window_ms=300, min_duration_ms=100)
    d = _feed(ep, [
        (100, "馬文播放晴天"), (250, "馬文播放晴天，"), (300, "馬文播放，晴天"),
        (420, "馬文播放晴天"),  # 距首次 320ms（標點變動不重置）→ 切
    ])
    assert d is not None
    assert d.revision_count == 0


# ── 空輸入 ──────────────────────────────────────────────────────────────────

def test_empty_text_never_cuts():
    ep = SemanticEndpointer(stability_window_ms=200, min_duration_ms=100)
    d = _feed(ep, [(100, ""), (400, ""), (700, "")])
    assert d is None


def test_reset_clears_state():
    ep = SemanticEndpointer(stability_window_ms=300, min_duration_ms=100)
    _feed(ep, [(100, "馬文"), (450, "馬文")])
    ep.reset()
    # reset 後新語句的時鐘從頭算
    d = ep.observe(500, "你好")
    assert d is None
