"""WakeSession 多輪查詢三修（2026-10-21 事故）：

1. session 內問答歷史進查詢上下文（wake_session.note_qa_turn / qa_history + grounded_answer
   session_history + run_grounded_qa 接線）。
2. session 黏著只接問句，非問句交給 Gemini 判「略」就安靜（NOT_A_QUESTION）。
3. session 內查詢直走付費 3.5-flash（不走免費、不 hedge）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import intent_agents.grounded_qa_agent as gqa
from intent_agents.grounded_qa_agent import GroundedQAAgent, grounded_answer
from intent_bus import IntentContext
from wake_session import MAX_QA_TURNS, WakeSession


def _ctx(query, speaker="A", mode="normal", dispatch_source="regex"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=(mode == "stream"),
        game_mode=(mode == "game"), is_owner=False, now=0.0, mode=mode,
        dispatch_source=dispatch_source,
    )


def _resp(text, *, chunks=1):
    chunk_objs = [SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))
                  for _ in range(chunks)]
    gm = SimpleNamespace(grounding_chunks=chunk_objs)
    cand = SimpleNamespace(grounding_metadata=gm, finish_reason="STOP")
    return SimpleNamespace(
        text=text, candidates=[cand],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50),
    )


def _client(resp=None, exc=None):
    cli = MagicMock()

    async def _gen(*, model, contents, config):
        if exc is not None:
            raise exc
        return resp

    cli.aio.models.generate_content = _gen
    return cli


def _guard(allow=True):
    g = MagicMock()
    g.allow.return_value = allow
    g.record = MagicMock()
    return g


# ── 1. wake_session 核心：note_qa_turn / qa_history ─────────────────────────

def test_note_qa_turn_only_target():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.note_qa_turn("B", "q", "a")
    assert s.qa_history("A") == []


def test_note_qa_turn_caps_at_max_turns():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    for i in range(MAX_QA_TURNS + 2):
        s.note_qa_turn("A", f"q{i}", f"a{i}")
    hist = s.qa_history("A")
    assert len(hist) == MAX_QA_TURNS
    assert hist[0] == (f"q{2}", f"a{2}")
    assert hist[-1] == (f"q{MAX_QA_TURNS + 1}", f"a{MAX_QA_TURNS + 1}")


def test_qa_history_empty_after_end():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.note_qa_turn("A", "q", "a")
    s._end("test")
    assert s.qa_history("A") == []


def test_qa_history_empty_after_target_change():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.note_qa_turn("A", "q", "a")
    s.on_wake_accepted("B", 10.0)
    assert s.qa_history("B") == []


def test_qa_history_non_target_returns_empty():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.note_qa_turn("A", "q", "a")
    assert s.qa_history("B") == []


# ── 2. grounded_answer 帶 session_history → 送入 contents ──────────────────

@pytest.mark.asyncio
async def test_grounded_answer_with_session_history_injects_before_current_question():
    captured = {}

    async def _gen(*, model, contents, config):
        captured["contents"] = contents
        return _resp("答案。")

    free = MagicMock()
    free.aio.models.generate_content = _gen
    out = await grounded_answer(
        free, None, _guard(), "那這個呢？",
        session_history=[("這個遊戲是什麼", "是涼亭區的遊戲")],
    )
    assert out is not None
    contents = captured["contents"]
    assert "問：這個遊戲是什麼" in contents
    assert "答：是涼亭區的遊戲" in contents
    qa_idx = contents.index("問：這個遊戲是什麼")
    cur_idx = contents.index("【使用者當前提問】")
    assert qa_idx < cur_idx


@pytest.mark.asyncio
async def test_grounded_answer_without_session_history_no_qa_segment():
    captured = {}

    async def _gen(*, model, contents, config):
        captured["contents"] = contents
        return _resp("答案。")

    free = MagicMock()
    free.aio.models.generate_content = _gen
    await grounded_answer(free, None, _guard(), "某個問題")
    assert "這次對話你剛回答過" not in captured["contents"]


# ── 3. 模型回「略」→ NOT_A_QUESTION ──────────────────────────────────────

@pytest.mark.asyncio
async def test_grounded_answer_returns_not_a_question_on_model_skip():
    free = _client(_resp("略"))
    out = await grounded_answer(free, None, _guard(), "那就是你的問題了")
    assert out is gqa.NOT_A_QUESTION


# ── 4. run_grounded_qa 收到 NOT_A_QUESTION ──────────────────────────────────

@pytest.mark.asyncio
async def test_run_grounded_qa_not_a_question_stays_silent(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    ctrl._wake_session_on_ignored = MagicMock()
    ctrl._wake_session_on_followup_result = MagicMock()

    monkeypatch.setattr(gqa, "grounded_answer", AsyncMock(return_value=gqa.NOT_A_QUESTION))
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "那就是你的問題了", source="session_followup")
    await asyncio.sleep(0)

    ctrl.play_tts.assert_not_awaited()
    ctrl._wake_session_on_ignored.assert_called_once_with("A")
    ctrl._wake_session_on_followup_result.assert_not_called()


# ── 5. run_grounded_qa 成功 → note_qa 被呼叫；查不到則不呼叫 ───────────────

@pytest.mark.asyncio
async def test_run_grounded_qa_notes_qa_turn_on_success(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    ctrl._wake_session_note_qa = MagicMock()

    monkeypatch.setattr(gqa, "grounded_answer",
                        AsyncMock(return_value=("答案是涼亭區。", ["example.com"])))
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "這個遊戲是什麼")
    await asyncio.sleep(0)

    ctrl._wake_session_note_qa.assert_called_once_with("A", "這個遊戲是什麼", "答案是涼亭區。")


@pytest.mark.asyncio
async def test_run_grounded_qa_no_answer_does_not_note_qa_turn(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    ctrl._wake_session_note_qa = MagicMock()

    monkeypatch.setattr(gqa, "grounded_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "查不到的東西")
    await asyncio.sleep(0)

    ctrl._wake_session_note_qa.assert_not_called()


# ── 6. run_grounded_qa 的 session 判定 → 直走付費 SESSION_MODEL_CHAIN ───────

@pytest.mark.asyncio
async def test_run_grounded_qa_session_followup_goes_paid_session_chain(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()

    mock_ga = AsyncMock(return_value=("答。", ["x"]))
    monkeypatch.setattr(gqa, "grounded_answer", mock_ga)
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "那他上一張呢", source="session_followup")
    await asyncio.sleep(0)

    args = mock_ga.call_args.args
    kwargs = mock_ga.call_args.kwargs
    assert args[0] is None
    assert kwargs["model_chain"] == gqa.SESSION_MODEL_CHAIN
    assert kwargs["timeout"] == gqa.SESSION_TIMEOUT_S
    assert kwargs["system_prompt"] is gqa._SESSION_SYSTEM_PROMPT
    assert "hedge_after_s" not in kwargs


@pytest.mark.asyncio
async def test_run_grounded_qa_captured_ambient_source_goes_paid_session_chain(monkeypatch):
    """source 預設("ambient_qa")但 ctrl._wake_session_captured 回 literal True
    → 仍視為 session 內，走付費 SESSION_MODEL_CHAIN（不帶 session system_prompt）。"""
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    ctrl._wake_session_captured = MagicMock(return_value=True)

    mock_ga = AsyncMock(return_value=("答。", ["x"]))
    monkeypatch.setattr(gqa, "grounded_answer", mock_ga)
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "某問題")
    await asyncio.sleep(0)

    args = mock_ga.call_args.args
    kwargs = mock_ga.call_args.kwargs
    assert args[0] is None
    assert kwargs["model_chain"] == gqa.SESSION_MODEL_CHAIN
    assert "system_prompt" not in kwargs


@pytest.mark.asyncio
async def test_run_grounded_qa_default_source_bare_mock_no_session_kwargs(monkeypatch):
    """source 預設("ambient_qa")且 ctrl 是裸 MagicMock（_wake_session_captured 回 truthy Mock，
    不是 literal True）→ 不視為 session 內，model_chain/timeout/system_prompt 都不傳，
    free_client 用 router.google_client。"""
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()

    mock_ga = AsyncMock(return_value=("答。", ["x"]))
    monkeypatch.setattr(gqa, "grounded_answer", mock_ga)
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "某問題")
    await asyncio.sleep(0)

    args = mock_ga.call_args.args
    kwargs = mock_ga.call_args.kwargs
    assert args[0] is ctrl.bot.router.google_client
    assert "model_chain" not in kwargs
    assert "timeout" not in kwargs
    assert "system_prompt" not in kwargs


# ── 7. run_grounded_qa 把 _wake_session_qa_history 的 list 當 session_history 傳下 ──

@pytest.mark.asyncio
async def test_run_grounded_qa_passes_qa_history_list(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    ctrl._wake_session_qa_history = MagicMock(return_value=[("q1", "a1")])

    mock_ga = AsyncMock(return_value=("答。", ["x"]))
    monkeypatch.setattr(gqa, "grounded_answer", mock_ga)
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "某問題")
    await asyncio.sleep(0)

    assert mock_ga.call_args.kwargs["session_history"] == [("q1", "a1")]


@pytest.mark.asyncio
async def test_run_grounded_qa_qa_history_non_list_becomes_empty(monkeypatch):
    """ctrl._wake_session_qa_history 是裸 MagicMock（回傳非 list）→ 傳空清單。"""
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()

    mock_ga = AsyncMock(return_value=("答。", ["x"]))
    monkeypatch.setattr(gqa, "grounded_answer", mock_ga)
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "某問題")
    await asyncio.sleep(0)

    assert mock_ga.call_args.kwargs["session_history"] == []


# ── 8. _session_followup_bid：問句門檻 ──────────────────────────────────────

def _agent(sticky: bool):
    ctrl = MagicMock()
    ctrl._wake_session_grounded_sticky = MagicMock(return_value=sticky)
    return GroundedQAAgent(ctrl), ctrl


@pytest.mark.parametrize("query", ["那就是你的問題了", "就自然不是有的"])
def test_session_followup_bid_rejects_non_questions(query):
    agent, _ = _agent(True)
    assert agent.bid(_ctx(query)).confidence == 0.0


@pytest.mark.parametrize("query", ["人民區的傳送點怎麼開", "房子舊的涼亭區域在哪裏"])
def test_session_followup_bid_accepts_questions(query):
    agent, _ = _agent(True)
    bid = agent.bid(_ctx(query))
    assert bid.confidence == GroundedQAAgent.SESSION_FOLLOWUP_CONFIDENCE


# ── 9. 付費記帳：思考 token + 搜尋費 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_paid_record_includes_thinking_tokens_and_search_fee():
    from llm_paid import estimate_cost

    chunk_objs = [SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))]
    gm = SimpleNamespace(grounding_chunks=chunk_objs, web_search_queries=["a", "b"])
    cand = SimpleNamespace(grounding_metadata=gm, finish_reason="STOP")
    resp = SimpleNamespace(
        text="答案。", candidates=[cand],
        usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=50, thoughts_token_count=200,
        ),
    )
    paid = _client(resp)
    guard = _guard()

    out = await grounded_answer(None, paid, guard, "問題", model_chain=("gemini-3.5-flash",))

    assert out is not None
    kwargs = guard.record.call_args.kwargs
    assert kwargs["out_tokens"] == 250
    assert kwargs["in_tokens"] == 100
    assert kwargs["est_usd"] == pytest.approx(
        estimate_cost("gemini-3.5-flash", 100, 250) + 2 * gqa.GROUNDING_USD_PER_SEARCH
    )


@pytest.mark.asyncio
async def test_paid_record_without_thinking_or_search_fields():
    from llm_paid import estimate_cost

    chunk_objs = [SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))]
    gm = SimpleNamespace(grounding_chunks=chunk_objs)
    cand = SimpleNamespace(grounding_metadata=gm, finish_reason="STOP")
    resp = SimpleNamespace(
        text="答案。", candidates=[cand],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50),
    )
    paid = _client(resp)
    guard = _guard()

    out = await grounded_answer(None, paid, guard, "問題", model_chain=("gemini-3.5-flash",))

    assert out is not None
    kwargs = guard.record.call_args.kwargs
    assert kwargs["out_tokens"] == 50
    assert kwargs["est_usd"] == pytest.approx(estimate_cost("gemini-3.5-flash", 100, 50))
