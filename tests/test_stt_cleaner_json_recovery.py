"""TDD: cleaner 截斷 JSON 救援。

6/2 數據：27 筆 cleaner LLM 回傳被切斷的 JSON（8b/免費模型）→ 整筆 parse 失敗
降級 raw = 白打一次 call。只要 cleaned 值完整就救回，避免浪費；cleaned 本身也截斷
才安全降級。
"""
from __future__ import annotations

from stt_cleaner import recover_truncated_cleaner_json as rec


# ── 可救回（cleaned 值完整）──────────────────────────────────────────────────

def test_recovers_cleaned_when_truncated_after_field():
    """'{"cleaned":"嘿Siri下下一首","intent":0.0,"cal' → 救回 cleaned + intent。"""
    out = rec('{"cleaned":"嘿Siri下下一首","intent":0.0,"cal')
    assert out is not None
    cleaned, intent, calling = out
    assert cleaned == "嘿Siri下下一首"
    assert intent == 0.0


def test_recovers_cleaned_when_intent_value_truncated():
    """intent 值本身被切（'0.'）→ cleaned 仍救回，intent=None。"""
    out = rec('{"cleaned":"繞口令繞口令繞口令","intent":0.')
    assert out is not None
    cleaned, intent, calling = out
    assert cleaned == "繞口令繞口令繞口令"
    assert intent is None


def test_recovers_with_calling_and_intent_clamped():
    out = rec('{"cleaned":"馬文你好","intent":1.5,"calling":true,"is_complete":true}')
    cleaned, intent, calling = out
    assert cleaned == "馬文你好"
    assert intent == 1.0          # clamp 到 1.0
    assert calling is True


# ── 不該救（cleaned 本身截斷 / 空）→ None，caller 降級 raw ─────────────────────

def test_returns_none_when_cleaned_value_truncated():
    """'{"cleaned":"你去運' 沒有結束引號 → 半截文字不可信，回 None。"""
    assert rec('{"cleaned":"你去運') is None


def test_returns_none_for_bare_brace():
    assert rec('{"') is None
    assert rec('{') is None


def test_returns_none_for_empty_cleaned():
    assert rec('{"cleaned":""') is None
    assert rec('{"cleaned":"   ","intent":0.0}') is None


def test_returns_none_for_garbage():
    assert rec('') is None
    assert rec('完全不是 json') is None
