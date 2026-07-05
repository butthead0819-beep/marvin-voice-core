"""TDD — Groq OpenAI-compat 400 防護：json_object 模式時 messages 必須含 'json'。

先紅後綠流程：
  - 未加 ensure_json_in_messages 之前，is_json=True 的 assert 全部紅
  - 實作 helper + 5 個呼叫點之後全綠

覆蓋範圍：
  A. ensure_json_in_messages 純函式（llm_json_compat.py）
  B. router legacy Groq Priority-1 路徑（gemini_router_llm.py）
  C. bus agent 路徑（OpenAICompatAgent 代表 groq/cerebras/openai_compat agents）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_json_compat import _JSON_HINT, ensure_json_in_messages
from llm_agents.base import LLMContext
from llm_agents.openai_compat_agent import OpenAICompatAgent
from llm_agents.quota_service import QuotaService
from llm_pool import CooldownAwarePool, PoolEndpoint


# ── Section A: helper 純函式 ─────────────────────────────────────────────────

def test_ensure_json_appends_hint_to_system_when_absent():
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "回傳結果"},
    ]
    result = ensure_json_in_messages(messages)
    assert any("json" in (m.get("content") or "").lower() for m in result)


def test_ensure_json_idempotent_when_system_already_has_json():
    orig_content = "請以 json 格式輸出結果"
    messages = [
        {"role": "system", "content": orig_content},
        {"role": "user", "content": "q"},
    ]
    result = ensure_json_in_messages(messages)
    assert result[0]["content"] == orig_content


def test_ensure_json_inserts_system_when_only_user_message():
    messages = [{"role": "user", "content": "回傳狀態"}]
    result = ensure_json_in_messages(messages)
    roles = [m["role"] for m in result]
    assert "system" in roles
    assert any("json" in (m.get("content") or "").lower() for m in result)


def test_ensure_json_returns_same_list_object():
    messages = [{"role": "user", "content": "x"}]
    result = ensure_json_in_messages(messages)
    assert result is messages


def test_ensure_json_idempotent_when_user_has_json():
    messages = [{"role": "user", "content": "give me json output"}]
    before_len = len(messages)
    result = ensure_json_in_messages(messages)
    assert len(result) == before_len


# ── Section B: router legacy Groq Priority-1 路徑攔截 ───────────────────────

def _make_router():
    from gemini_router_llm import GeminiRouterLLMMixin
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = '{"ok":1}'
    mock_resp.usage = MagicMock(total_tokens=10)

    obj = GeminiRouterLLMMixin.__new__(GeminiRouterLLMMixin)
    obj.groq_simple_model = "groq-m"
    obj.groq_fallback_model = "groq-m"
    obj.groq_dedicated_client = MagicMock()
    obj.groq_dedicated_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    obj.current_tier = "Tier-1"
    obj.on_fallback_callback = None
    obj.model_name = "test"
    obj.budget = MagicMock(add_tokens=MagicMock())
    obj.is_exhausted = True
    obj.cerebras_client = None
    obj.cerebras_model = None
    return obj


@pytest.mark.asyncio
async def test_router_groq_json_true_messages_contain_json(monkeypatch):
    monkeypatch.delenv("LLM_BUS", raising=False)
    router = _make_router()
    await router._call_llm("你是助手", "回傳狀態", is_json=True, tier="simple")
    kwargs = router.groq_dedicated_client.chat.completions.create.call_args.kwargs
    messages = kwargs["messages"]
    assert any("json" in (m.get("content") or "").lower() for m in messages), \
        "is_json=True 時 create() 收到的 messages 必須含字面 'json'"
    assert kwargs.get("response_format") == {"type": "json_object"}


@pytest.mark.asyncio
async def test_router_groq_json_false_no_hint_injected(monkeypatch):
    monkeypatch.delenv("LLM_BUS", raising=False)
    router = _make_router()
    await router._call_llm("你是助手", "回傳狀態", is_json=False, tier="simple")
    kwargs = router.groq_dedicated_client.chat.completions.create.call_args.kwargs
    messages = kwargs["messages"]
    assert not any(_JSON_HINT in (m.get("content") or "") for m in messages)
    assert kwargs.get("response_format") is None


# ── Section C: bus agent 路徑攔截 ────────────────────────────────────────────

def _make_quota(provider="together"):
    clock_holder = [1000.0]
    clock = lambda: clock_holder[0]  # noqa: E731
    ep_q = PoolEndpoint(name=f"{provider}-quick", client=MagicMock(),
                        model=f"{provider}-quick-model", tpm_budget=6000)
    ep_a = PoolEndpoint(name=f"{provider}-analyze", client=MagicMock(),
                        model=f"{provider}-analyze-model", tpm_budget=6000)
    pool = CooldownAwarePool([ep_q, ep_a], clock=clock)
    return QuotaService([pool]), ep_q, ep_a


def _chat_response(content="hi", total_tokens=10):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock(total_tokens=total_tokens)
    return resp


@pytest.mark.asyncio
async def test_openai_compat_agent_json_true_messages_contain_json():
    quota, ep_q, _ = _make_quota()
    ep_q.client.chat.completions.create = AsyncMock(return_value=_chat_response('{"a":1}'))
    agent = OpenAICompatAgent(quota, provider_name="together")
    await agent.handle(LLMContext(prompt="q", purpose="cleaner", json_mode=True))
    kwargs = ep_q.client.chat.completions.create.call_args.kwargs
    messages = kwargs["messages"]
    assert any("json" in (m.get("content") or "").lower() for m in messages), \
        "json_mode=True 時 create() 收到的 messages 必須含 'json'"
    assert kwargs.get("response_format") == {"type": "json_object"}


@pytest.mark.asyncio
async def test_openai_compat_agent_json_false_no_injection():
    quota, ep_q, _ = _make_quota()
    ep_q.client.chat.completions.create = AsyncMock(return_value=_chat_response("plain"))
    agent = OpenAICompatAgent(quota, provider_name="together")
    await agent.handle(LLMContext(prompt="q", purpose="cleaner", json_mode=False))
    kwargs = ep_q.client.chat.completions.create.call_args.kwargs
    messages = kwargs["messages"]
    assert not any(_JSON_HINT in (m.get("content") or "") for m in messages)
    assert "response_format" not in kwargs
