"""對談查詢前先「整理問題」（design 2026-10-23 拍板）。

語音問題常聽糊、省略主詞：先用 3.5-flash（預設思考）把原句整理成一句獨立明確的問題
再查，再用整理後的問題讀回給使用者當 ack。見 intent_agents/grounded_qa_agent.py
build_rewrite_input / rewrite_question / readback_text / run_grounded_qa 接線。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import intent_agents.grounded_qa_agent as gqa
from intent_agents.grounded_qa_agent import (
    build_rewrite_input, readback_text, rewrite_question,
)


# ── build_rewrite_input ──────────────────────────────────────────────────────

def test_build_rewrite_input_full():
    out = build_rewrite_input(
        "是二代還是一代",
        speaker="showay",
        recent_context="A: 我們在玩 Grounded 2\nB: 水下武器怎麼做",
        session_history=[("上一題", "上一答")],
        games=["Grounded 2"],
    )
    assert "【大家最近在玩的遊戲】Grounded 2" in out
    assert "【過去 10 分鐘語音對話背景】\nA: 我們在玩 Grounded 2\nB: 水下武器怎麼做" in out
    assert "【這次對話 Marvin 剛回答過的問答】\n問：上一題\n答：上一答" in out
    assert "【提問人】showay\n【使用者原句】是二代還是一代" in out
    # 段落順序
    idx_games = out.index("【大家最近在玩的遊戲】")
    idx_ctx = out.index("【過去 10 分鐘語音對話背景】")
    idx_hist = out.index("【這次對話 Marvin 剛回答過的問答】")
    idx_user = out.index("【提問人】")
    assert idx_games < idx_ctx < idx_hist < idx_user


def test_build_rewrite_input_has_no_glossary_segment():
    """10/23：舊 LLM 生成術語表混一代用語、13 題實測有無無差 → 拿掉，等從整理紀錄長出用語表。"""
    out = build_rewrite_input(
        "幫註怎麼做", speaker="showay", recent_context="", session_history=[],
        games=["Grounded 2"],
    )
    assert "常見術語" not in out
    assert out == "【大家最近在玩的遊戲】Grounded 2\n\n【提問人】showay\n【使用者原句】幫註怎麼做"


def test_build_rewrite_input_all_empty_only_user_segment():
    out = build_rewrite_input(
        "馬文幾歲", speaker="showay", recent_context="", session_history=[], games=[],
    )
    assert out == "【提問人】showay\n【使用者原句】馬文幾歲"


# ── rewrite_question ──────────────────────────────────────────────────────────

def _rewrite_resp(question: str, basis: str = ""):
    return MagicMock(
        text=json.dumps({"question": question, "basis": basis}, ensure_ascii=False),
        usage_metadata=MagicMock(prompt_token_count=200, candidates_token_count=40, thoughts_token_count=10),
    )


def _rewrite_client(resp=None, exc=None, side_effect=None):
    cli = MagicMock()
    if side_effect is not None:
        cli.aio.models.generate_content = AsyncMock(side_effect=side_effect)
    elif exc is not None:
        cli.aio.models.generate_content = AsyncMock(side_effect=exc)
    else:
        cli.aio.models.generate_content = AsyncMock(return_value=resp)
    return cli


def _guard(allow=True):
    g = MagicMock()
    g.allow.return_value = allow
    g.record = MagicMock()
    return g


@pytest.mark.asyncio
async def test_rewrite_question_success_records_usage():
    client = _rewrite_client(_rewrite_resp("Grounded 2 的水下武器怎麼做？"))
    guard = _guard()
    out = await rewrite_question(client, guard, "幫註怎麼做", speaker="showay",
                                  recent_context="", session_history=[], games=["Grounded 2"])
    assert out == "Grounded 2 的水下武器怎麼做？"
    guard.record.assert_called_once()
    assert guard.record.call_args.kwargs["caller"] == "qa_rewriter"
    assert guard.record.call_args.kwargs["in_tokens"] == 200
    assert guard.record.call_args.kwargs["out_tokens"] == 50


@pytest.mark.asyncio
async def test_rewrite_question_not_a_question_returns_empty_string():
    client = _rewrite_client(_rewrite_resp(""))
    out = await rewrite_question(client, _guard(), "欸你看那隻狗", speaker="showay",
                                  recent_context="", session_history=[], games=[])
    assert out == ""


@pytest.mark.asyncio
async def test_rewrite_question_exception_returns_none():
    client = _rewrite_client(exc=RuntimeError("boom"))
    out = await rewrite_question(client, _guard(), "幫註怎麼做", speaker="showay",
                                  recent_context="", session_history=[], games=[])
    assert out is None


@pytest.mark.asyncio
async def test_rewrite_question_timeout_returns_none():
    async def _hang(*a, **kw):
        await asyncio.sleep(999)

    client = _rewrite_client(side_effect=_hang)
    with patch.object(gqa, "REWRITE_TIMEOUT_S", 0.01):
        out = await rewrite_question(client, _guard(), "幫註怎麼做", speaker="showay",
                                      recent_context="", session_history=[], games=[])
    assert out is None


@pytest.mark.asyncio
async def test_rewrite_question_bad_json_returns_none():
    resp = MagicMock(text="不是json", usage_metadata=MagicMock(
        prompt_token_count=10, candidates_token_count=10, thoughts_token_count=0))
    client = _rewrite_client(resp)
    out = await rewrite_question(client, _guard(), "幫註怎麼做", speaker="showay",
                                  recent_context="", session_history=[], games=[])
    assert out is None


@pytest.mark.asyncio
async def test_rewrite_question_env_disabled_no_call(monkeypatch):
    monkeypatch.setenv("MARVIN_QA_REWRITE", "0")
    client = _rewrite_client(_rewrite_resp("x"))
    out = await rewrite_question(client, _guard(), "幫註怎麼做", speaker="showay",
                                  recent_context="", session_history=[], games=[])
    assert out is None
    client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewrite_question_no_client_returns_none():
    out = await rewrite_question(None, _guard(), "幫註怎麼做", speaker="showay",
                                  recent_context="", session_history=[], games=[])
    assert out is None


@pytest.mark.asyncio
async def test_rewrite_question_guard_capped_returns_none():
    client = _rewrite_client(_rewrite_resp("x"))
    out = await rewrite_question(client, _guard(allow=False), "幫註怎麼做", speaker="showay",
                                  recent_context="", session_history=[], games=[])
    assert out is None
    client.aio.models.generate_content.assert_not_awaited()


# ── readback_text ────────────────────────────────────────────────────────────

def test_readback_text_same_after_stripping_punctuation_returns_none():
    assert readback_text("Grounded 2 怎麼做", "Grounded 2 怎麼做？") is None


def test_readback_text_too_long_returns_none():
    long_q = "一" * 41
    assert readback_text("原句", long_q) is None


def test_readback_text_builds_sentence():
    out = readback_text("幫註怎麼做", "Grounded 2 的水下武器怎麼做？")
    assert out == "你是問Grounded 2 的水下武器怎麼做嗎？我查一下。"


# ── run_grounded_qa 接線 ──────────────────────────────────────────────────────

def _ctrl():
    ctrl = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()
    return ctrl


@pytest.mark.asyncio
async def test_run_grounded_qa_rewrite_success_uses_rewritten_and_readback():
    ctrl = _ctrl()
    with patch.object(gqa, "rewrite_question", new=AsyncMock(return_value="Grounded 2 的水下武器怎麼做？")), \
         patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=("答案", ["src"]))) as mock_answer, \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "showay", "幫註怎麼做")

    assert mock_answer.call_args.args[3] == "Grounded 2 的水下武器怎麼做？"
    assert mock_answer.call_args.kwargs["game_context"] is None

    calls = [c.args[0] for c in ctrl.play_tts.call_args_list]
    assert calls[0] == "你是問Grounded 2 的水下武器怎麼做嗎？我查一下。"
    assert calls[1] == "答案"


@pytest.mark.asyncio
async def test_run_grounded_qa_rewrite_none_falls_back_to_original_and_games():
    ctrl = _ctrl()
    with patch.object(gqa, "rewrite_question", new=AsyncMock(return_value=None)), \
         patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=("答案", ["src"]))) as mock_answer, \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=["Grounded 2"]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "showay", "幫註怎麼做")

    assert mock_answer.call_args.args[3] == "幫註怎麼做"
    assert mock_answer.call_args.kwargs["game_context"] == ["Grounded 2"]
    assert len(ctrl.play_tts.call_args_list) == 1  # 沒有讀回句


@pytest.mark.asyncio
async def test_run_grounded_qa_rewrite_empty_question_opener_returns_not_question():
    ctrl = _ctrl()
    with patch.object(gqa, "rewrite_question", new=AsyncMock(return_value="")), \
         patch.object(gqa, "grounded_answer", new=AsyncMock()) as mock_answer, \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa") as mock_record:
        status = await gqa.run_grounded_qa(ctrl, "showay", "欸你看那隻狗", source="question_opener")

    assert status == "not_question"
    mock_answer.assert_not_awaited()
    assert mock_record.call_args[0][0]["reason"] == "not_question"


@pytest.mark.asyncio
async def test_run_grounded_qa_rewrite_empty_ambient_qa_still_uses_original():
    ctrl = _ctrl()
    with patch.object(gqa, "rewrite_question", new=AsyncMock(return_value="")), \
         patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=("答案", ["src"]))) as mock_answer, \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        status = await gqa.run_grounded_qa(ctrl, "showay", "幫註怎麼做", source="ambient_qa")

    assert status == "answered"
    assert mock_answer.call_args.args[3] == "幫註怎麼做"


@pytest.mark.asyncio
async def test_run_grounded_qa_record_ambient_qa_includes_rewritten_field():
    ctrl = _ctrl()
    with patch.object(gqa, "rewrite_question", new=AsyncMock(return_value="整理後的問題")), \
         patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=("答案", ["src"]))), \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa") as mock_record:
        await gqa.run_grounded_qa(ctrl, "showay", "原句")

    rec = mock_record.call_args[0][0]
    assert rec["rewritten"] == "整理後的問題"


@pytest.mark.asyncio
async def test_run_grounded_qa_follow_up_skips_rewrite():
    """10/23 使用者定：只整理第一句。對談裡已有前幾輪問答（追問）→ 不整理，原句＋上下文給 lite。"""
    ctrl = _ctrl()
    ctrl._wake_session_qa_history = MagicMock(return_value=[("Grounded 2 的綠背椿象在哪？", "在橋樑區。")])
    rw = AsyncMock(return_value="不該被用到")
    with patch.object(gqa, "rewrite_question", new=rw), \
         patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=("答案", ["src"]))) as mock_answer, \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=["Grounded 2"]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "showay", "是二代還是一代", source="session_followup")

    rw.assert_not_awaited()
    assert mock_answer.call_args.args[3] == "是二代還是一代"
    assert mock_answer.call_args.kwargs["session_history"] == [("Grounded 2 的綠背椿象在哪？", "在橋樑區。")]
    assert [c.args[0] for c in ctrl.play_tts.call_args_list] == ["答案"]   # 沒有讀回句
