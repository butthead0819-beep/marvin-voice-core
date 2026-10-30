"""問句型開場句交給 3.5-flash 判斷（2026-10-21 定案）。

10/6 以來 149 句「馬文＋開場句」，27 句沒 agent 接，其中 7 句是真問題但說法不合
regex 句型（「臺積電今天收盤股價多少」「Apple TV 臺灣什麼時候開賣」…）。使用者定案：
不再補句型，問句形狀的開場句交給 3.5-flash 自己判斷要不要查；不是查詢就回「略」交回
Marvin 主聊天。見 intent_agents/grounded_qa_agent.py 的 try_question_opener。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import intent_agents.grounded_qa_agent as gqa
from intent_agents.grounded_qa_agent import parse_grounded_qa, try_question_opener


def _ctx(query, speaker="A", raw_text=None, mode="normal"):
    return SimpleNamespace(
        speaker=speaker, query=query,
        raw_text=raw_text if raw_text is not None else query,
        mode=mode,
    )


def _guard(allow=True):
    g = MagicMock()
    g.allow.return_value = allow
    g.record = MagicMock()
    return g


# ── 1. run_grounded_qa 回傳狀態 → try_question_opener 布林轉換 ─────────────

@pytest.mark.asyncio
async def test_opener_calls_run_grounded_qa_with_question_opener_source(monkeypatch):
    mock_rgqa = AsyncMock(return_value="answered")
    monkeypatch.setattr(gqa, "run_grounded_qa", mock_rgqa)

    ctrl = MagicMock()
    ctx = _ctx("臺積電今天收盤股價多少", raw_text="馬文臺積電今天收盤股價多少")
    result = await try_question_opener(ctrl, ctx)

    assert result is True
    mock_rgqa.assert_awaited_once_with(
        ctrl, "A", "臺積電今天收盤股價多少",
        raw="馬文臺積電今天收盤股價多少", source="question_opener",
    )


@pytest.mark.asyncio
async def test_opener_no_answer_still_true(monkeypatch):
    monkeypatch.setattr(gqa, "run_grounded_qa", AsyncMock(return_value="no_answer"))
    ctrl = MagicMock()
    result = await try_question_opener(ctrl, _ctx("臺積電今天收盤股價多少"))
    assert result is True


@pytest.mark.asyncio
async def test_opener_not_question_returns_false(monkeypatch):
    monkeypatch.setattr(gqa, "run_grounded_qa", AsyncMock(return_value="not_question"))
    ctrl = MagicMock()
    result = await try_question_opener(ctrl, _ctx("臺積電今天收盤股價多少"))
    assert result is False


@pytest.mark.asyncio
async def test_opener_marks_start_and_done_even_on_early_not_question_exit():
    """E1 打點：mode 不相容等早退路徑（不呼叫 run_grounded_qa）也要標 opener_start/opener_done，
    否則早退佔多數的 not_question case 會在分段時間裡看起來「沒進開場判斷」。"""
    import pipeline_timing

    pipeline_timing.start()
    ctrl = MagicMock()
    result = await try_question_opener(ctrl, _ctx("隨便聊聊", mode="game"))

    assert result is False
    snap = pipeline_timing.snapshot()
    assert "opener_start" in snap
    assert "opener_done" in snap


# ── 2. 剝喚醒詞前綴 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_opener_strips_wake_prefix(monkeypatch):
    mock_rgqa = AsyncMock(return_value="answered")
    monkeypatch.setattr(gqa, "run_grounded_qa", mock_rgqa)

    ctrl = MagicMock()
    ctx = _ctx("馬文，臺積電今天收盤股價多少")
    await try_question_opener(ctrl, ctx)

    assert mock_rgqa.call_args.args[2] == "臺積電今天收盤股價多少"


# ── 3. 不呼叫 run_grounded_qa 且回 False ───────────────────────────────────

# 意圖漏斗步驟 2 刪除：test_opener_low_confidence_wake_skips（low_confidence_wake
# 全鏈已拔，try_question_opener 不再檢查它）。


@pytest.mark.parametrize("query", [
    "我覺得還好",
    "播周杰倫的晴天",
    "這首歌是什麼",
    "嗎",
])
@pytest.mark.asyncio
async def test_opener_excluded_or_non_question_skips(monkeypatch, query):
    mock_rgqa = AsyncMock(return_value="answered")
    monkeypatch.setattr(gqa, "run_grounded_qa", mock_rgqa)

    ctrl = MagicMock()
    result = await try_question_opener(ctrl, _ctx(query))

    assert result is False
    mock_rgqa.assert_not_awaited()


# ── 4. 今天真實開場句都要觸發呼叫 ───────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "臺積電今天收盤股價多少",
    "2026年的 Apple TV臺灣什麼時候開賣",
    "你知道 Gronede2這個遊戲嗎",
    "小客車的前輪跟後輪氣壓差多少",
])
@pytest.mark.asyncio
async def test_opener_real_openers_trigger_run_grounded_qa(monkeypatch, query):
    mock_rgqa = AsyncMock(return_value="answered")
    monkeypatch.setattr(gqa, "run_grounded_qa", mock_rgqa)

    ctrl = MagicMock()
    await try_question_opener(ctrl, _ctx(query))

    mock_rgqa.assert_awaited_once()


@pytest.mark.parametrize("query", [
    "你懂意思嗎馬文在這邊錶錶",
    "馬文你有聽到嗎",
    "馬文你聽得懂嗎",
])
@pytest.mark.asyncio
async def test_opener_excludes_second_person_check_phrases(monkeypatch, query):
    mock_rgqa = AsyncMock(return_value="answered")
    monkeypatch.setattr(gqa, "run_grounded_qa", mock_rgqa)

    ctrl = MagicMock()
    result = await try_question_opener(ctrl, _ctx(query))

    assert result is False
    mock_rgqa.assert_not_called()


# ── 5. run_grounded_qa 本體：source="question_opener" 的行為 ───────────────

def _resp(text, *, chunks=1):
    chunk_objs = [SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))
                  for _ in range(chunks)]
    gm = SimpleNamespace(grounding_chunks=chunk_objs)
    cand = SimpleNamespace(grounding_metadata=gm, finish_reason="STOP")
    return SimpleNamespace(
        text=text, candidates=[cand],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50),
    )


def _mk_ctrl():
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    ctrl._wake_session_on_ignored = MagicMock()
    ctrl._wake_session_on_followup_result = MagicMock()
    ctrl._wake_session_note_grounded = MagicMock()
    return ctrl


@pytest.mark.asyncio
async def test_run_grounded_qa_question_opener_uses_opener_prompt_and_session_chain(monkeypatch):
    ctrl = _mk_ctrl()
    mock_ga = AsyncMock(return_value=("答。", ["x"]))
    monkeypatch.setattr(gqa, "grounded_answer", mock_ga)
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "臺積電今天收盤股價多少", source="question_opener")
    await asyncio.sleep(0)

    args = mock_ga.call_args.args
    kwargs = mock_ga.call_args.kwargs
    assert args[0] is None
    assert kwargs["system_prompt"] is gqa._OPENER_SYSTEM_PROMPT
    assert kwargs["model_chain"] == gqa.QA_MODEL_CHAIN


@pytest.mark.asyncio
async def test_run_grounded_qa_question_opener_not_a_question_returns_to_chat(monkeypatch):
    ctrl = _mk_ctrl()
    monkeypatch.setattr(gqa, "grounded_answer", AsyncMock(return_value=gqa.NOT_A_QUESTION))
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    status = await gqa.run_grounded_qa(ctrl, "A", "我覺得還好", source="question_opener")
    await asyncio.sleep(0)

    assert status == "not_question"
    ctrl.play_tts.assert_not_awaited()
    ctrl._wake_session_on_ignored.assert_not_called()
    ctrl._wake_session_note_grounded.assert_not_called()


@pytest.mark.asyncio
async def test_run_grounded_qa_question_opener_answered_returns_answered(monkeypatch):
    ctrl = _mk_ctrl()
    monkeypatch.setattr(gqa, "grounded_answer", AsyncMock(return_value=("答案。", ["x.com"])))
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    status = await gqa.run_grounded_qa(ctrl, "A", "臺積電今天收盤股價多少", source="question_opener")
    await asyncio.sleep(0)

    assert status == "answered"
    ctrl._wake_session_note_grounded.assert_called_once_with("A")


@pytest.mark.asyncio
async def test_run_grounded_qa_question_opener_no_answer_returns_no_answer(monkeypatch):
    ctrl = _mk_ctrl()
    monkeypatch.setattr(gqa, "grounded_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    status = await gqa.run_grounded_qa(ctrl, "A", "臺積電今天收盤股價多少", source="question_opener")
    await asyncio.sleep(0)

    assert status == "no_answer"
    ctrl._wake_session_note_grounded.assert_called_once_with("A")


@pytest.mark.asyncio
async def test_run_grounded_qa_session_followup_not_a_question_unchanged(monkeypatch):
    """10/21：NOT_A_QUESTION 一律交回聊天（不再呼叫 _wake_session_on_ignored 安靜吞掉）。"""
    ctrl = _mk_ctrl()
    monkeypatch.setattr(gqa, "grounded_answer", AsyncMock(return_value=gqa.NOT_A_QUESTION))
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    status = await gqa.run_grounded_qa(ctrl, "A", "那就是你的問題了", source="session_followup")
    await asyncio.sleep(0)

    assert status == "not_question"
    ctrl._wake_session_on_ignored.assert_not_called()


@pytest.mark.asyncio
async def test_run_grounded_qa_default_source_notes_grounded_once_at_start_only(monkeypatch):
    """預設 source 成功回答 → _wake_session_note_grounded 只在開頭被呼叫一次（出口不重複）。"""
    ctrl = _mk_ctrl()
    monkeypatch.setattr(gqa, "grounded_answer", AsyncMock(return_value=("答案。", ["x.com"])))
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "某問題")
    await asyncio.sleep(0)

    ctrl._wake_session_note_grounded.assert_called_once_with("A")


# ── 6. _LOOKUP_RE 只認句首（2026-10-21 事故：句中「查詢」被誤當指令） ───────

@pytest.mark.parametrize("query", [
    "馬文我有一個疑問我進入了那個輪巡查詢之後還可以嗎",
    "馬文對他的降級會不會是馬不是查詢降級",
])
def test_lookup_re_does_not_match_mid_sentence_lookup_verb(query):
    assert parse_grounded_qa(query) is None


@pytest.mark.parametrize("query", [
    "馬文幫我查光點兔的三級鏟子材料去哪裡拿",
    "馬文你幫我查一下台積電股價",
    "馬文 我想知道黑神話悟空評價好不好",
    "幫我查 Grandy Two遊戲裡面的水下武器要怎麼製作",
])
def test_lookup_re_matches_sentence_initial_lookup_verb(query):
    assert parse_grounded_qa(query) is not None


# ── 7. try_question_opener sticky=True 但模型判定非查詢 → 交回聊天（False） ──

@pytest.mark.asyncio
async def test_opener_sticky_true_not_question_returns_false(monkeypatch):
    monkeypatch.setattr(gqa, "run_grounded_qa", AsyncMock(return_value="not_question"))
    ctrl = MagicMock()
    ctrl._wake_session_grounded_sticky = MagicMock(return_value=True)

    result = await try_question_opener(ctrl, _ctx("臺積電今天收盤股價多少"))

    assert result is False


# ── 8. run_grounded_qa 全部 source 都一律直走付費 QA_MODEL_CHAIN、free_client=None ──

@pytest.mark.parametrize("source", ["game_knowledge", "ambient_qa", "music_followup"])
@pytest.mark.asyncio
async def test_run_grounded_qa_all_sources_use_paid_qa_chain(monkeypatch, source):
    ctrl = _mk_ctrl()
    mock_ga = AsyncMock(return_value=("答。", ["x"]))
    monkeypatch.setattr(gqa, "grounded_answer", mock_ga)
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "某問題", source=source)
    await asyncio.sleep(0)

    args = mock_ga.call_args.args
    kwargs = mock_ga.call_args.kwargs
    assert args[0] is None
    assert kwargs["model_chain"] == gqa.QA_MODEL_CHAIN
