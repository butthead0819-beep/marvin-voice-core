"""build_rescue_components — env-gated factory 把 LLM + sink 組起來。

voice_controller 唯一接觸點：呼叫這個工廠，把結果三元組塞進 IntentBus。

env 變數契約：
  MARVIN_INTENT_RESCUE_ENABLED=1  → 開啟整條 rescue pipeline（預設 OFF，安全）
  MARVIN_INTENT_RESCUE_SHADOW=0   → 顯式關 shadow（預設 ON，校準週用）

回 (None, False, None) 的情境：
- env 未開啟
- tier_router 是 None（pool 都沒 key / 啟動失敗）
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intent_agents.llm_rescue_agent import LLMRescueAgent
from intent_agents.rescue_classifier import build_rescue_components


def test_returns_none_triple_when_env_disabled():
    """預設安全：沒設 MARVIN_INTENT_RESCUE_ENABLED → 完全不啟用，IntentBus 等同舊行為。"""
    agent, shadow, sink = build_rescue_components(MagicMock(), env={})
    assert agent is None
    assert shadow is False
    assert sink is None


def test_returns_none_triple_when_env_set_to_zero():
    """顯式 =0 也算 disabled，跟 J2 shadow env 慣例一致。"""
    agent, shadow, sink = build_rescue_components(
        MagicMock(), env={"MARVIN_INTENT_RESCUE_ENABLED": "0"}
    )
    assert agent is None


def test_returns_components_when_env_enabled():
    """env=1 → 完整三元組；shadow 預設 ON（校準週）。"""
    agent, shadow, sink = build_rescue_components(
        MagicMock(), env={"MARVIN_INTENT_RESCUE_ENABLED": "1"}
    )
    assert isinstance(agent, LLMRescueAgent)
    assert shadow is True  # default shadow on
    assert callable(sink)


def test_shadow_can_be_explicitly_disabled():
    """校準週後手動關 shadow → rescue 真的影響對話路徑。"""
    env = {"MARVIN_INTENT_RESCUE_ENABLED": "1", "MARVIN_INTENT_RESCUE_SHADOW": "0"}
    _, shadow, _ = build_rescue_components(MagicMock(), env=env)
    assert shadow is False


def test_returns_none_triple_when_tier_router_missing():
    """所有 LLM provider 都沒 key → tier_router=None → 不該嘗試組 rescue agent。"""
    env = {"MARVIN_INTENT_RESCUE_ENABLED": "1"}
    agent, shadow, sink = build_rescue_components(None, env=env)
    assert agent is None
    assert shadow is False
    assert sink is None


def test_sink_writes_to_records_rescue_outcomes_jsonl(tmp_path, monkeypatch):
    """sink 預設寫 records/rescue_outcomes.jsonl —— daily ritual 在那邊找。
    用 monkeypatch chdir 到 tmp_path 驗證實際寫到 cwd-relative 路徑。"""
    monkeypatch.chdir(tmp_path)
    _, _, sink = build_rescue_components(
        MagicMock(), env={"MARVIN_INTENT_RESCUE_ENABLED": "1"}
    )
    sink({"gap_class": "shadow", "original_query": "x", "rewritten_query": "y",
          "winner_agent": None, "winner_reason": None, "pragmatic_signal": None,
          "pragmatic_target": None, "speaker": "Alice", "ts": 0.0})

    expected = tmp_path / "records" / "rescue_outcomes.jsonl"
    assert expected.exists()
    assert "shadow" in expected.read_text()
