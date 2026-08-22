"""AudioRescueAgent.synthesize() — mock google_client，不打真 Gemini。

契約（同 intent_agents/audio_rescue_agent.py docstring）：
- ctx.audio_wav_bytes 空 → 完全不呼叫 Gemini，直接 None
- 選中 action tool → 回傳 dispatch_source="llm_rescue_audio" 的 ctx，帶 resolved_*
- 多個平行 action tool call → 只取第一個，其餘忽略（記 log）
- 唯讀 tool call → 呼叫 readonly_tool_executor，不映射進 resolved_*
- Gemini 逾時 / 例外 / 沒有任何 function_call → None
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext
from intent_agents.audio_rescue_agent import AudioRescueAgent


def _ctx(audio: bytes | None = b"fake-wav-bytes", query="小聲一點"):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode="normal", audio_wav_bytes=audio,
    )


def _manifest():
    return {
        "version": "x",
        "agents": [{"name": "volume", "intents": [
            {"name": "volume_down", "required_slots": [], "reason_template": "x"},
        ]}],
    }


class _FakeCall:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


def _make_client(function_calls=None, raise_exc=None, hang=False):
    client = MagicMock()

    async def _generate(*args, **kwargs):
        if hang:
            await asyncio.sleep(10)
        if raise_exc:
            raise raise_exc
        response = MagicMock()
        response.function_calls = function_calls or []
        return response

    client.aio.models.generate_content = AsyncMock(side_effect=_generate)
    return client


@pytest.mark.asyncio
async def test_no_audio_bytes_skips_gemini_call_entirely():
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")])
    agent = AudioRescueAgent(google_client=client, manifest_provider=_manifest)

    result = await agent.synthesize(_ctx(audio=None))

    assert result is None
    client.aio.models.generate_content.assert_not_called()


@pytest.mark.asyncio
async def test_action_tool_call_resolves_agent_and_intent():
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")])
    agent = AudioRescueAgent(google_client=client, manifest_provider=_manifest)

    result = await agent.synthesize(_ctx())

    assert result is not None
    assert result.dispatch_source == "llm_rescue_audio"
    assert result.resolved_agent == "volume"
    assert result.resolved_intent == "volume_down"
    assert result.depth == 1


@pytest.mark.asyncio
async def test_multiple_action_calls_only_first_resolved():
    calls = [_FakeCall("volume__volume_down"), _FakeCall("volume__volume_up")]
    client = _make_client(function_calls=calls)
    agent = AudioRescueAgent(google_client=client, manifest_provider=_manifest)

    result = await agent.synthesize(_ctx())

    assert result.resolved_intent == "volume_down"


@pytest.mark.asyncio
async def test_readonly_tool_call_invokes_executor_and_not_resolved():
    client = _make_client(function_calls=[_FakeCall("get_now_playing")])
    executor = AsyncMock()
    agent = AudioRescueAgent(
        google_client=client, manifest_provider=_manifest, readonly_tool_executor=executor
    )

    result = await agent.synthesize(_ctx())

    assert result is None
    executor.assert_awaited_once()
    assert executor.await_args.args[0] == "get_now_playing"


@pytest.mark.asyncio
async def test_readonly_and_action_call_mixed():
    calls = [_FakeCall("get_now_playing"), _FakeCall("volume__volume_down")]
    client = _make_client(function_calls=calls)
    executor = AsyncMock()
    agent = AudioRescueAgent(
        google_client=client, manifest_provider=_manifest, readonly_tool_executor=executor
    )

    result = await agent.synthesize(_ctx())

    executor.assert_awaited_once()
    assert result.resolved_intent == "volume_down"


@pytest.mark.asyncio
async def test_gemini_timeout_returns_none():
    client = _make_client(hang=True)
    agent = AudioRescueAgent(
        google_client=client, manifest_provider=_manifest, timeout_s=0.01
    )

    result = await agent.synthesize(_ctx())

    assert result is None


@pytest.mark.asyncio
async def test_gemini_exception_returns_none():
    client = _make_client(raise_exc=RuntimeError("gateway down"))
    agent = AudioRescueAgent(google_client=client, manifest_provider=_manifest)

    result = await agent.synthesize(_ctx())

    assert result is None


@pytest.mark.asyncio
async def test_no_function_call_returns_none():
    client = _make_client(function_calls=[])
    agent = AudioRescueAgent(google_client=client, manifest_provider=_manifest)

    result = await agent.synthesize(_ctx())

    assert result is None


@pytest.mark.asyncio
async def test_malformed_tool_call_name_ignored_not_crashed():
    client = _make_client(function_calls=[_FakeCall("no_separator")])
    agent = AudioRescueAgent(google_client=client, manifest_provider=_manifest)

    result = await agent.synthesize(_ctx())

    assert result is None
