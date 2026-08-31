"""build_rescue_components — env-gated factory 把 LLM + sink + wav_store 組起來。

voice_controller 唯一接觸點：呼叫這個工廠，把結果四元組塞進 IntentBus。

env 變數契約：
  MARVIN_INTENT_RESCUE_ENABLED=1  → 開啟整條 rescue pipeline（預設 OFF，安全）
  MARVIN_INTENT_RESCUE_MODE=audio → 原始 wav + Gemini function calling（預設 text）
  MARVIN_INTENT_RESCUE_SHADOW=0   → 顯式關 shadow（預設 ON，校準週用）

回 (None, False, None, None) 的情境：
- env 未開啟
- text mode 且 tier_router 是 None（pool 都沒 key / 啟動失敗）
- audio mode 且 google_client / manifest_provider 缺一
"""
from __future__ import annotations

from unittest.mock import MagicMock

from intent_agents.audio_rescue_agent import AudioRescueAgent
from intent_agents.llm_rescue_agent import LLMRescueAgent
from intent_agents.rescue_classifier import build_rescue_components
from intent_agents.rescue_outcome_logger import RescueWavStore


def test_returns_none_tuple_when_env_disabled():
    """預設安全：沒設 MARVIN_INTENT_RESCUE_ENABLED → 完全不啟用，IntentBus 等同舊行為。"""
    agent, shadow, sink, wav_store = build_rescue_components(MagicMock(), env={})
    assert agent is None
    assert shadow is False
    assert sink is None
    assert wav_store is None


def test_returns_none_tuple_when_env_set_to_zero():
    agent, *_ = build_rescue_components(
        MagicMock(), env={"MARVIN_INTENT_RESCUE_ENABLED": "0"}
    )
    assert agent is None


def test_returns_components_when_env_enabled():
    """env=1 → 完整四元組；text mode 預設無 wav_store；shadow 預設 ON。"""
    agent, shadow, sink, wav_store = build_rescue_components(
        MagicMock(), env={"MARVIN_INTENT_RESCUE_ENABLED": "1"}
    )
    assert isinstance(agent, LLMRescueAgent)
    assert shadow is True
    assert callable(sink)
    assert wav_store is None  # text mode 不需要 wav sidecar


def test_shadow_can_be_explicitly_disabled():
    env = {"MARVIN_INTENT_RESCUE_ENABLED": "1", "MARVIN_INTENT_RESCUE_SHADOW": "0"}
    _, shadow, _, _ = build_rescue_components(MagicMock(), env=env)
    assert shadow is False


def test_returns_none_tuple_when_tier_router_missing():
    env = {"MARVIN_INTENT_RESCUE_ENABLED": "1"}
    agent, shadow, sink, wav_store = build_rescue_components(None, env=env)
    assert (agent, shadow, sink, wav_store) == (None, False, None, None)


def test_sink_writes_to_records_rescue_outcomes_jsonl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, _, sink, _ = build_rescue_components(
        MagicMock(), env={"MARVIN_INTENT_RESCUE_ENABLED": "1"}
    )
    sink({"gap_class": "shadow", "original_query": "x", "rewritten_query": "y",
          "winner_agent": None, "winner_reason": None, "pragmatic_signal": None,
          "pragmatic_target": None, "speaker": "Alice", "ts": 0.0})

    expected = tmp_path / "records" / "rescue_outcomes.jsonl"
    assert expected.exists()
    assert "shadow" in expected.read_text()


# ── Audio Rescue v2：MARVIN_INTENT_RESCUE_MODE ──────────────────────────────

def test_mode_unset_behaves_exactly_like_before():
    """MODE 未設 → 預設 "text"，行為與 Audio Rescue v2 上線前完全一致（回歸）。"""
    agent, _, _, wav_store = build_rescue_components(
        MagicMock(), env={"MARVIN_INTENT_RESCUE_ENABLED": "1"}
    )
    assert isinstance(agent, LLMRescueAgent)
    assert wav_store is None


def test_mode_text_explicit_uses_llm_rescue_agent():
    env = {"MARVIN_INTENT_RESCUE_ENABLED": "1", "MARVIN_INTENT_RESCUE_MODE": "text"}
    agent, _, _, _ = build_rescue_components(MagicMock(), env=env)
    assert isinstance(agent, LLMRescueAgent)


def test_mode_audio_without_google_client_degrades_gracefully():
    env = {"MARVIN_INTENT_RESCUE_ENABLED": "1", "MARVIN_INTENT_RESCUE_MODE": "audio"}
    result = build_rescue_components(
        MagicMock(), env=env, google_client=None, manifest_provider=lambda: {},
    )
    assert result == (None, False, None, None)


def test_mode_audio_without_manifest_provider_degrades_gracefully():
    env = {"MARVIN_INTENT_RESCUE_ENABLED": "1", "MARVIN_INTENT_RESCUE_MODE": "audio"}
    agent, *_ = build_rescue_components(
        MagicMock(), env=env, google_client=MagicMock(), manifest_provider=None,
    )
    assert agent is None


def test_mode_audio_with_deps_returns_agent_and_wav_store():
    env = {"MARVIN_INTENT_RESCUE_ENABLED": "1", "MARVIN_INTENT_RESCUE_MODE": "audio"}
    agent, shadow, sink, wav_store = build_rescue_components(
        MagicMock(), env=env, google_client=MagicMock(), manifest_provider=lambda: {},
    )
    assert isinstance(agent, AudioRescueAgent)
    assert shadow is True
    assert callable(sink)
    assert isinstance(wav_store, RescueWavStore)  # audio mode 才有 wav sidecar
