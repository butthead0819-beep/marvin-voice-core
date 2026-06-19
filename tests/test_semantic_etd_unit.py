"""
_apply_semantic_etd 單元測試 —— 驅動「真的」VoiceController 方法（非 test_etd_pipeline
的手抄副本）。

把 handle_stt_result 內的雙軌語意終止偵測（Track B-1 啟發式 / B-2 Groq / 硬門檻）
抽成 _apply_semantic_etd 後，這組固定其契約：
  - 回傳 None        → 句子未完成，已緩衝 + 排硬門檻 flush，caller 應 return
  - 回傳 (text, ts)  → 句子完成 / 達結算長度，caller 以此續跑

行為不變的整體保證來自完整套件；本檔補真方法的細節覆蓋。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_vc(*, clean_result=None):
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.user_sentence_buffer = {}
    vc.bot = MagicMock()
    vc.bot.router.clean_stt_text = AsyncMock(return_value=clean_result)
    return vc


async def _call(vc, speaker, text, ts=100.0):
    return await vc._apply_semantic_etd(
        speaker, text, ts, prosody_data=None, wav_bytes=b"", track="B",
    )


def _cancel_pending(vc, speaker):
    buf = vc.user_sentence_buffer.get(speaker)
    if buf and buf.get("task"):
        buf["task"].cancel()


# ── B-1 啟發式：思考拖延詞 → 未完成、緩衝、不諮詢 Groq ────────────────────────
@pytest.mark.asyncio
async def test_thinking_word_buffers_and_skips_groq():
    vc = _make_vc()
    result = await _call(vc, "陳進文", "我要點歌然後")
    assert result is None                               # 已緩衝
    assert "陳進文" in vc.user_sentence_buffer
    assert vc.user_sentence_buffer["陳進文"]["texts"] == ["我要點歌然後"]
    vc.bot.router.clean_stt_text.assert_not_awaited()   # B-1 命中即短路 B-2
    _cancel_pending(vc, "陳進文")


# ── B-2 Groq 判定未完成 → 緩衝、回 None ──────────────────────────────────────
@pytest.mark.asyncio
async def test_groq_incomplete_buffers():
    vc = _make_vc(clean_result={"is_complete": False})
    result = await _call(vc, "陳進文", "幫我查")
    assert result is None
    assert "陳進文" in vc.user_sentence_buffer
    vc.bot.router.clean_stt_text.assert_awaited_once()
    _cancel_pending(vc, "陳進文")


# ── B-2 Groq 判定完成 → 解析、buffer 清空、回 (text, ts) ──────────────────────
@pytest.mark.asyncio
async def test_groq_complete_resolves():
    vc = _make_vc(clean_result={"is_complete": True})
    result = await _call(vc, "陳進文", "幫我查天氣", ts=123.0)
    assert result == ("幫我查天氣", 123.0)
    assert "陳進文" not in vc.user_sentence_buffer       # 已 pop


# ── 結尾標點 → B-1 不觸發；B-2 無 is_complete key → 視為完成 ──────────────────
@pytest.mark.asyncio
async def test_punctuated_sentence_resolves_when_groq_silent():
    vc = _make_vc(clean_result={})        # 沒有 is_complete key → is_complete 維持 True
    result = await _call(vc, "陳進文", "播周杰倫的歌。", ts=50.0)
    assert result == ("播周杰倫的歌。", 50.0)


# ── 累積：先緩衝一句，再來一句完成 → 合併兩句一起回傳 ────────────────────────
@pytest.mark.asyncio
async def test_accumulation_combines_buffered_texts():
    vc = _make_vc(clean_result={"is_complete": False})
    r1 = await _call(vc, "陳進文", "我要")
    assert r1 is None                                    # 第一句緩衝
    # 第二句讓 Groq 判完成
    vc.bot.router.clean_stt_text = AsyncMock(return_value={"is_complete": True})
    r2 = await _call(vc, "陳進文", "點歌")
    assert r2 == ("我要，點歌", 100.0)                    # 合併 + 用最初時間戳
    assert "陳進文" not in vc.user_sentence_buffer


# ── 硬門檻：累積達 5 句即使未完成也強制結算 ──────────────────────────────────
@pytest.mark.asyncio
async def test_hard_threshold_forces_resolve_at_five():
    vc = _make_vc(clean_result={"is_complete": False})
    # 預塞 4 句
    vc.user_sentence_buffer["陳進文"] = {
        "texts": ["a", "b", "c", "d"], "task": None,
        "timestamp": 10.0, "prosody_data": None,
    }
    result = await _call(vc, "陳進文", "e")               # 第 5 句 → len=5 不 <5
    assert result == ("a，b，c，d，e", 10.0)
    assert "陳進文" not in vc.user_sentence_buffer
