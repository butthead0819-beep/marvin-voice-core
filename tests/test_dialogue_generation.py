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
    segments = await generate_dual_dialogue(content_text="現在幾點", llm_fn=llm_fn)
    assert segments is not None
    assert len(segments) == 2
    assert segments[0]["voice"] == "marvin"
    assert segments[1]["voice"] == "marmo"


@pytest.mark.asyncio
async def test_llm_raises_returns_none():
    llm_fn = AsyncMock(side_effect=RuntimeError("timeout"))
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_llm_returns_bad_json_returns_none():
    llm_fn = _llm_returns("這不是 JSON")
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_llm_returns_json_without_segments_key_returns_none():
    llm_fn = _llm_returns({"marvin": "x", "marmo": "y"})  # wrong schema
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
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
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
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
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
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
    segments = await generate_dual_dialogue(content_text="現在幾點", llm_fn=llm_fn)
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
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
    assert segments is None


@pytest.mark.asyncio
async def test_llm_called_with_marmo_text_in_user_prompt():
    """marmo_text 應該被注入到 user-side prompt（不是 system）。"""
    llm_fn = _llm_returns({"segments": [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ]})
    await generate_dual_dialogue(content_text="找到了第 7083 行", llm_fn=llm_fn)
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
    await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
    system_prompt = llm_fn.call_args.args[0]
    assert "馬文" in system_prompt
    assert "馬末" in system_prompt
    # 必須有 boke-tsukkomi / 跑題-打斷 pattern 提示
    assert "打斷" in system_prompt or "跑題" in system_prompt


# ── pattern="marmo_lead" 順序變 [marmo, marvin] ──────────────────────────────

@pytest.mark.asyncio
async def test_marmo_lead_pattern_reorders_marmo_first():
    """marmo_lead pattern → 即使 LLM 把 marvin 放前面，也要 reorder 成 [marmo, marvin]。"""
    llm_fn = _llm_returns({
        "segments": [
            {"voice": "marvin", "text": "存在仍是虛無"},
            {"voice": "marmo", "text": "找到了第 7083 行"},
        ]
    })
    segments = await generate_dual_dialogue(
        content_text="找到了第 7083 行", llm_fn=llm_fn, pattern="marmo_lead"
    )
    assert segments is not None
    assert segments[0]["voice"] == "marmo"
    assert segments[1]["voice"] == "marvin"


@pytest.mark.asyncio
async def test_marmo_lead_pattern_uses_lead_prompt():
    """marmo_lead pattern → system prompt 含「Marmo 先講」pattern 描述。"""
    llm_fn = _llm_returns({"segments": [
        {"voice": "marmo", "text": "a"},
        {"voice": "marvin", "text": "b"},
    ]})
    await generate_dual_dialogue(content_text="x", llm_fn=llm_fn, pattern="marmo_lead")
    system_prompt = llm_fn.call_args.args[0]
    # marmo_lead block only 的特徵字（Marvin 小題大作）
    assert "小題大作" in system_prompt
    # marvin_lead block 特徵字（漫才 ツッコミ 公式）不該出現
    assert "複述＋點破" not in system_prompt


@pytest.mark.asyncio
async def test_marvin_lead_pattern_default_unchanged():
    """預設 pattern=marvin_lead → 順序 [marvin, marmo]、prompt 含 marvin_lead block。"""
    llm_fn = _llm_returns({"segments": [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ]})
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
    assert segments[0]["voice"] == "marvin"
    assert segments[1]["voice"] == "marmo"
    # marvin_lead block only 的特徵字（漫才 ツッコミ 公式）
    assert "複述＋點破" in llm_fn.call_args.args[0]


@pytest.mark.asyncio
async def test_strips_speaker_label_prefix_from_text():
    """LLM 把 speaker 標籤 echo 進 text（"Marvin: ..." / "馬末：..."）→ 清掉，TTS 不念。"""
    llm_fn = _llm_returns({
        "segments": [
            {"voice": "marvin", "text": "Marvin: Marmo 的三封信化作宇宙哀嘆"},
            {"voice": "marmo", "text": "馬末：我整理好了"},
        ]
    })
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
    texts = {s["voice"]: s["text"] for s in segments}
    assert texts["marvin"] == "Marmo 的三封信化作宇宙哀嘆"
    assert texts["marmo"] == "我整理好了"


@pytest.mark.asyncio
async def test_strips_repeated_speaker_prefix():
    """疊多層標籤（"Marvin: Marmo: ..."）也要清乾淨。"""
    llm_fn = _llm_returns({
        "segments": [
            {"voice": "marvin", "text": "Marvin:Marmo: 真的假的"},
            {"voice": "marmo", "text": "正常一句"},
        ]
    })
    segments = await generate_dual_dialogue(content_text="x", llm_fn=llm_fn)
    texts = {s["voice"]: s["text"] for s in segments}
    assert texts["marvin"] == "真的假的"


@pytest.mark.asyncio
async def test_marvin_lead_includes_manzai_tension_release():
    """marvin_lead 要含漫才核心「緊張緩和」技法 + ツッコミ 公式。"""
    llm_fn = _llm_returns({"segments": [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ]})
    await generate_dual_dialogue(content_text="x", llm_fn=llm_fn, pattern="marvin_lead")
    sp = llm_fn.call_args.args[0]
    assert "緊張緩和" in sp
    assert "ツッコミ" in sp


@pytest.mark.asyncio
async def test_both_patterns_include_naming_rule():
    """兩種 pattern 都要含「角色互稱規則」——反應者要點名對方。"""
    llm_fn = _llm_returns({"segments": [
        {"voice": "marvin", "text": "a"},
        {"voice": "marmo", "text": "b"},
    ]})
    for pattern in ("marvin_lead", "marmo_lead"):
        llm_fn.reset_mock()
        await generate_dual_dialogue(content_text="x", llm_fn=llm_fn, pattern=pattern)
        sp = llm_fn.call_args.args[0]
        assert "角色互稱規則" in sp
        assert "點名" in sp
