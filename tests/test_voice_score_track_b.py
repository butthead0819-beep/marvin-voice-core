"""
TDD：P2 — _voice_score 在 track="B" 且 wake_intent 中段 (0.65-1.0) 時
不該把 LLM 判定的意圖丟掉。

問題：原本只有 wake_intent < 0.65 時才把 wake_intent 當 voice score；
≥ 0.65 時 fall through 到 fast/force_intervene 的硬編碼 1.0/0.95，
LLM 對「不確定但不否決」的判斷完全被結構性 regex 蓋過。

意圖層面：
- Track A (wake_intent=None) = 純 regex 路徑，1.0 / 0.95 合理
- Track B (wake_intent 有值) = LLM 已被叫來判定意圖；LLM 的 verdict
  應該優先於 regex 結構訊號（regex 只說「結構像 wake」，LLM 說
  「這話是不是對 Marvin 說的」）。Mid-range LLM verdict 應該被
  保留進 voice channel。
"""
from __future__ import annotations

import pytest

from wake_detector import WakeDetector


# ── Track B：wake_intent 應該主導 voice score（不受 regex action 蓋過）─

@pytest.mark.parametrize("wake_intent", [0.65, 0.70, 0.75, 0.80, 0.90, 0.95])
def test_track_b_mid_high_intent_uses_wake_intent_not_regex_max(wake_intent):
    """Track B 且 wake_intent ≥ 0.65 → 應該回 wake_intent，不該硬給 1.0。"""
    voice = WakeDetector._voice_score(
        action="fast_intervene", wake_intent=wake_intent, track="B"
    )
    assert voice == pytest.approx(wake_intent), \
        f"track=B wake_intent={wake_intent} 應該保留 LLM 判定，實際={voice}"


@pytest.mark.parametrize("wake_intent", [0.65, 0.80, 0.95])
def test_track_b_force_intervene_uses_wake_intent_not_095(wake_intent):
    """Track B + force_intervene 也該用 wake_intent 不該硬給 0.95。"""
    voice = WakeDetector._voice_score(
        action="force_intervene", wake_intent=wake_intent, track="B"
    )
    assert voice == pytest.approx(wake_intent)


def test_track_b_low_intent_still_returns_wake_intent_baseline():
    """既有行為：Track B + wake_intent < 0.65 仍回 wake_intent（LLM veto）。"""
    voice = WakeDetector._voice_score(
        action="fast_intervene", wake_intent=0.30, track="B"
    )
    assert voice == pytest.approx(0.30)


# ── Track A / 無 track：regex 路徑保留既有行為 ─────────────────────────────

def test_track_a_fast_intervene_returns_1_baseline():
    """Track A regex 路徑：wake_intent=None，純 regex 給 1.0。"""
    voice = WakeDetector._voice_score(
        action="fast_intervene", wake_intent=None, track="A"
    )
    assert voice == 1.0


def test_no_track_fast_intervene_returns_1_baseline():
    """無 track 標記 + wake_intent=None：純 regex 給 1.0。"""
    voice = WakeDetector._voice_score(
        action="fast_intervene", wake_intent=None, track=None
    )
    assert voice == 1.0


def test_no_track_force_intervene_returns_095_baseline():
    voice = WakeDetector._voice_score(
        action="force_intervene", wake_intent=None, track=None
    )
    assert voice == 0.95


# ── llm_verify 路徑：直接用 wake_intent（既有） ──────────────────────────

def test_llm_verify_uses_wake_intent():
    voice = WakeDetector._voice_score(
        action="llm_verify", wake_intent=0.55, track="B"
    )
    assert voice == pytest.approx(0.55)


# ── drop / 無 wake：回 0 ──────────────────────────────────────────────────

def test_drop_with_no_intent_returns_zero():
    voice = WakeDetector._voice_score(
        action="drop", wake_intent=None, track=None
    )
    assert voice == 0.0


def test_drop_with_intent_returns_scaled():
    """drop + wake_intent 有值 → 視為弱訊號，乘 0.6（既有 fallback）。"""
    voice = WakeDetector._voice_score(
        action="drop", wake_intent=0.5, track=None
    )
    assert voice == pytest.approx(0.5 * 0.6)
