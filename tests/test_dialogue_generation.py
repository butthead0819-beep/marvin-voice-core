"""services/dialogue_generation.py — Marvin+Marmo dual segments LLM 生成。

驗證：
  - Happy path：LLM 回 valid JSON → 回 segments list [marvin, marmo]
  - LLM 例外 → None
  - LLM 回 bad JSON → None
  - 紅線命中 → None（drop dual，caller fallback 單 Marvin）
  - 順序強制 [marvin, marmo]：LLM 把 marmo 放前面也會被 reorder（功能位差 hardcode）
  - 空 marmo_text → 還是 call LLM（caller 上游應該擋，但函式不該炸）
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from services.dialogue_generation import (
    RED_LINE_KEYWORDS,
    generate_dual_dialogue,
)


def _llm_returns(payload: dict | str):
    """Build an AsyncMock llm_fn that returns the given payload as raw text."""
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return AsyncMock(return_value=raw)


@pytest.mark.asyncio
async def test_happy_path_returns_two_segments():
    llm_fn = _llm_returns({
        "segments": [
            {"voice": "marvin", "text": "時間。又是時間。"},
            {"voice": "marmo", "text": "閉嘴，下午三點四十二。"},
        ]
    })
    segments = await generate_dual_dialogue(marmo_text="現在幾點", llm_fn=llm_fn)
    assert segments is not None
    assert len(segments) == 2
    assert segments[0]["voice"] == "marvin"
    assert segments[1]["voice"] == "marmo"


@pytest.mark.asyncio
async def test_llm_raises_returns_none():
    llm_fn = AsyncMock(side_effect=RuntimeError("timeout"))
    segments = await generate_dual_dialogue(marmo_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_llm_returns_bad_json_returns_none():
    llm_fn = _llm_returns("這不是 JSON")
    segments = await generate_dual_dialogue(marmo_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_llm_returns_json_without_segments_key_returns_none():
    llm_fn = _llm_returns({"marvin": "x", "marmo": "y"})  # wrong schema
    segments = await generate_dual_dialogue(marmo_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_red_line_word_in_marmo_segment_returns_none():
    # Pick a real red-line keyword and assert filter trips
    bad_word = next(iter(RED_LINE_KEYWORDS))
    llm_fn = _llm_returns({
        "segments": [
            {"voice": "marvin", "text": "好的。"},
            {"voice": "marmo", "text": f"你這個 {bad_word}"},
        ]
    })
    segments = await generate_dual_dialogue(marmo_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_red_line_word_in_marvin_segment_returns_none():
    bad_word = next(iter(RED_LINE_KEYWORDS))
    llm_fn = _llm_returns({
        "segments": [
            {"voice": "marvin", "text": f"你是個 {bad_word}"},
            {"voice": "marmo", "text": "好的。"},
        ]
    })
    segments = await generate_dual_dialogue(marmo_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_order_enforced_marvin_first():
    """LLM 把 marmo 放前面、marvin 放後面 → 函式 reorder 成 [marvin, marmo]
    （功能位差：boke 必須先發、tsukkomi 必須後）。"""
    llm_fn = _llm_returns({
        "segments": [
            {"voice": "marmo", "text": "閉嘴，下午三點四十二。"},
            {"voice": "marvin", "text": "時間。又是時間。"},
        ]
    })
    segments = await generate_dual_dialogue(marmo_text="現在幾點", llm_fn=llm_fn)
    assert segments is not None
    assert segments[0]["voice"] == "marvin"
    assert segments[1]["voice"] == "marmo"


@pytest.mark.asyncio
async def test_missing_voice_label_returns_none():
    """segment 沒有 voice 欄位 / 不是 marvin/marmo → None。"""
    llm_fn = _llm_returns({
        "segments": [
            {"voice": "marvin", "text": "a"},
            {"text": "no voice"},  # missing voice
        ]
    })
    segments = await generate_dual_dialogue(marmo_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_llm_called_with_marmo_text_in_user_prompt():
    """marmo_text 應該被注入到 user-side prompt（不是 system）。"""
    llm_fn = _llm_returns({"segments": [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ]})
    await generate_dual_dialogue(marmo_text="找到了第 7083 行", llm_fn=llm_fn)
    call_args = llm_fn.call_args
    # llm_fn signature: (system_prompt: str, user_prompt: str) -> str
    assert "找到了第 7083 行" in call_args.args[1]


@pytest.mark.asyncio
async def test_system_prompt_includes_both_personas():
    """system prompt 應該包 Marvin + Marmo 兩個 persona context。"""
    llm_fn = _llm_returns({"segments": [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ]})
    await generate_dual_dialogue(marmo_text="x", llm_fn=llm_fn)
    system_prompt = llm_fn.call_args.args[0]
    assert "馬文" in system_prompt
    assert "馬末" in system_prompt
    # 必須有 boke-tsukkomi / 跑題-打斷 pattern 提示
    assert "打斷" in system_prompt or "跑題" in system_prompt
