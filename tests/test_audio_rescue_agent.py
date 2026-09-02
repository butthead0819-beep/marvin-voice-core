"""AudioRescueAgent.synthesize() — mock google_client，不打真 Gemini。

契約（同 intent_agents/audio_rescue_agent.py docstring）：
- ctx.audio_wav_bytes 空 → 完全不呼叫 Gemini，直接 None
- 選中 action tool → 回傳 dispatch_source="llm_rescue_audio" 的 ctx，帶 resolved_*
- 多個平行 action tool call → 只取第一個，其餘忽略（記 log）
- 唯讀 tool call → 呼叫 readonly_tool_executor，不映射進 resolved_*
- Gemini 逾時 / 例外 / 沒有任何 function_call → None
- 付費鐵則：呼叫前 guard.allow() 守門 + RPM 視窗守門，成功後 guard.record() 記帳
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
    # opt-in：manifest_to_function_declarations 只曝有 description 的 intent。
    return {
        "version": "x",
        "agents": [{"name": "volume", "intents": [
            {"name": "volume_down", "required_slots": [], "reason_template": "x",
             "description": "調小聲"},
        ]}],
    }


class _FakeCall:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


class _FakeGuard:
    """PaidUsageGuard 替身：不碰磁碟，只記呼叫供斷言。allow 預設放行。"""
    def __init__(self, allow_result=True):
        self.allow_result = allow_result
        self.allow_calls: list[float] = []
        self.record_calls: list[dict] = []

    def allow(self, expected_usd):
        self.allow_calls.append(expected_usd)
        return self.allow_result

    def record(self, **kwargs):
        self.record_calls.append(kwargs)


def _make_client(function_calls=None, raise_exc=None, hang=False, usage_metadata=None):
    client = MagicMock()

    async def _generate(*args, **kwargs):
        if hang:
            await asyncio.sleep(10)
        if raise_exc:
            raise raise_exc
        response = MagicMock()
        response.function_calls = function_calls or []
        response.usage_metadata = usage_metadata
        return response

    client.aio.models.generate_content = AsyncMock(side_effect=_generate)
    return client


def _make_agent(client, **kwargs):
    guard = kwargs.pop("paid_guard", None) or _FakeGuard()
    return AudioRescueAgent(
        google_client=client, manifest_provider=_manifest, paid_guard=guard, **kwargs
    )


@pytest.mark.asyncio
async def test_no_audio_bytes_skips_gemini_call_entirely():
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")])
    agent = _make_agent(client)

    result = await agent.synthesize(_ctx(audio=None))

    assert result is None
    client.aio.models.generate_content.assert_not_called()


@pytest.mark.asyncio
async def test_filler_query_still_calls_gemini():
    """8/22 放寬：has_intent_signal 過濾拿掉了，語氣詞一樣會送——成本量到無感，不再擋語意。"""
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")])
    agent = _make_agent(client)

    await agent.synthesize(_ctx(query="嗯"))

    client.aio.models.generate_content.assert_called_once()


@pytest.mark.asyncio
async def test_action_tool_call_resolves_agent_and_intent():
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")])
    agent = _make_agent(client)

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
    agent = _make_agent(client)

    result = await agent.synthesize(_ctx())

    assert result.resolved_intent == "volume_down"


@pytest.mark.asyncio
async def test_readonly_tool_call_invokes_executor_and_not_resolved():
    client = _make_client(function_calls=[_FakeCall("get_now_playing")])
    executor = AsyncMock()
    agent = _make_agent(client, readonly_tool_executor=executor)

    result = await agent.synthesize(_ctx())

    assert result is None
    executor.assert_awaited_once()
    assert executor.await_args.args[0] == "get_now_playing"


@pytest.mark.asyncio
async def test_abstain_tool_call_returns_none():
    """LLM 選 just_chatting（棄權出口）→ 放棄 rescue，走一般聊天。"""
    from intent_agents.audio_rescue_tools import ABSTAIN_TOOL_NAME
    client = _make_client(function_calls=[_FakeCall(ABSTAIN_TOOL_NAME)])
    agent = _make_agent(client)

    result = await agent.synthesize(_ctx())

    assert result is None
    assert agent.last_abandon_reason == "just_chatting"


@pytest.mark.asyncio
async def test_last_abandon_reason_tracks_branch_and_clears_on_success():
    """各放棄分支記對原因；成功執行時清 None（IntentBus emit abandoned outcome 用）。"""
    from intent_agents.audio_rescue_tools import ABSTAIN_TOOL_NAME

    ag = _make_agent(_make_client(function_calls=[]))
    await ag.synthesize(_ctx())
    assert ag.last_abandon_reason == "no_tool_calls"

    ag = _make_agent(_make_client(raise_exc=RuntimeError("gateway down")))
    await ag.synthesize(_ctx())
    assert ag.last_abandon_reason.startswith("gemini_error")

    ag = _make_agent(_make_client(function_calls=[_FakeCall("volume__volume_down")]))
    ok = await ag.synthesize(_ctx())
    assert ok is not None
    assert ag.last_abandon_reason is None


@pytest.mark.asyncio
async def test_abstain_wins_over_other_calls_in_same_response():
    """just_chatting 出現在 call list 裡就直接放棄，不理其他 action call。"""
    from intent_agents.audio_rescue_tools import ABSTAIN_TOOL_NAME
    calls = [_FakeCall(ABSTAIN_TOOL_NAME), _FakeCall("volume__volume_down")]
    client = _make_client(function_calls=calls)
    agent = _make_agent(client)

    assert await agent.synthesize(_ctx()) is None


@pytest.mark.asyncio
async def test_abstain_declaration_included_in_gemini_tools():
    from intent_agents.audio_rescue_tools import ABSTAIN_TOOL_NAME
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")])
    agent = _make_agent(client)

    await agent.synthesize(_ctx())

    cfg = client.aio.models.generate_content.await_args.kwargs["config"]
    tool_names = {fd.name for fd in cfg.tools[0].function_declarations}
    assert ABSTAIN_TOOL_NAME in tool_names


@pytest.mark.asyncio
async def test_readonly_and_action_call_mixed():
    calls = [_FakeCall("get_now_playing"), _FakeCall("volume__volume_down")]
    client = _make_client(function_calls=calls)
    executor = AsyncMock()
    agent = _make_agent(client, readonly_tool_executor=executor)

    result = await agent.synthesize(_ctx())

    executor.assert_awaited_once()
    assert result.resolved_intent == "volume_down"


@pytest.mark.asyncio
async def test_gemini_timeout_returns_none():
    client = _make_client(hang=True)
    agent = _make_agent(client, timeout_s=0.01)

    result = await agent.synthesize(_ctx())

    assert result is None


@pytest.mark.asyncio
async def test_gemini_exception_returns_none():
    client = _make_client(raise_exc=RuntimeError("gateway down"))
    agent = _make_agent(client)

    result = await agent.synthesize(_ctx())

    assert result is None


@pytest.mark.asyncio
async def test_no_function_call_returns_none():
    client = _make_client(function_calls=[])
    agent = _make_agent(client)

    result = await agent.synthesize(_ctx())

    assert result is None


@pytest.mark.asyncio
async def test_malformed_tool_call_name_ignored_not_crashed():
    client = _make_client(function_calls=[_FakeCall("no_separator")])
    agent = _make_agent(client)

    result = await agent.synthesize(_ctx())

    assert result is None


# ── 付費鐵則：guard.allow() 前擋 / guard.record() 後記 / RPM 視窗 ──────────────

@pytest.mark.asyncio
async def test_guard_denies_skips_gemini_call_entirely():
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")])
    guard = _FakeGuard(allow_result=False)
    agent = _make_agent(client, paid_guard=guard)

    result = await agent.synthesize(_ctx())

    assert result is None
    client.aio.models.generate_content.assert_not_called()
    assert len(guard.allow_calls) == 1


@pytest.mark.asyncio
async def test_successful_call_records_usage_with_real_token_counts():
    usage = MagicMock(prompt_token_count=123, candidates_token_count=45)
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")], usage_metadata=usage)
    guard = _FakeGuard()
    agent = _make_agent(client, paid_guard=guard)

    await agent.synthesize(_ctx())

    assert len(guard.record_calls) == 1
    rec = guard.record_calls[0]
    assert rec["caller"] == "audio_rescue"
    assert rec["in_tokens"] == 123
    assert rec["out_tokens"] == 45


@pytest.mark.asyncio
async def test_successful_call_falls_back_to_estimate_when_no_usage_metadata():
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")], usage_metadata=None)
    guard = _FakeGuard()
    agent = _make_agent(client, paid_guard=guard)

    await agent.synthesize(_ctx(audio=b"x" * 32000))  # 1 秒份量

    assert len(guard.record_calls) == 1
    rec = guard.record_calls[0]
    assert rec["in_tokens"] == 32  # 1 秒 * 32 token/秒


@pytest.mark.asyncio
async def test_failed_call_does_not_record_usage():
    client = _make_client(raise_exc=RuntimeError("boom"))
    guard = _FakeGuard()
    agent = _make_agent(client, paid_guard=guard)

    await agent.synthesize(_ctx())

    assert guard.record_calls == []


@pytest.mark.asyncio
async def test_rpm_window_exhausted_skips_call():
    client = _make_client(function_calls=[_FakeCall("volume__volume_down")])
    agent = _make_agent(client)
    agent._RPM_LIMIT = 1
    await agent.synthesize(_ctx())  # 吃掉唯一一個 slot

    result = await agent.synthesize(_ctx())

    assert result is None
    client.aio.models.generate_content.assert_called_once()  # 第二次沒真的打出去


def test_default_rpm_limit_is_relaxed_to_twenty():
    """8/22 放寬：RPM 上限只當失控迴圈天花板，不擋正常對話節奏。"""
    agent = AudioRescueAgent(google_client=MagicMock(), manifest_provider=_manifest)
    assert agent._RPM_LIMIT == 20


def test_default_paid_guard_has_relaxed_caps():
    """8/22 放寬：單次呼叫成本量到無感（$0.00002-0.00003），cap 拉高到 GCP 帳戶量級。"""
    agent = AudioRescueAgent(google_client=MagicMock(), manifest_provider=_manifest)
    assert agent.paid_guard.daily_cap_usd == 2.0
    assert agent.paid_guard.monthly_cap_usd == 10.0
