"""NemoClaw 掩飾語測試（2026-06-13，frame 設計）。

設計：LLM 出含 {Q} 的句型框架，主體由系統從原句確定性套入 → LLM 碰不到答案，
結構上不可能洩漏。框架本身過數字/英文 backstop（LLM 別在框架塞事實）。
"""
from __future__ import annotations

import asyncio

from nemoclaw_cover import (
    build_cover, extract_subject, frame_is_safe, safe_fallback_cover, generate_cover,
)


# ── extract_subject：剝命令詞、保留主體（原句的詞）──────────────────────────

def test_extract_strips_lead_command():
    assert extract_subject("幫我查蕭煌奇是什麼時候出生") == "蕭煌奇是什麼時候出生"
    assert extract_subject("幫我找高雄哈爾濱街的酒吧") == "高雄哈爾濱街的酒吧"


def test_extract_keeps_subject_when_no_command():
    assert extract_subject("怎麼去蘭嶼啊") == "怎麼去蘭嶼啊".rstrip("啊")


def test_extract_never_empty():
    assert extract_subject("查") != ""


# ── frame_is_safe ────────────────────────────────────────────────────────────

def test_frame_needs_exactly_one_placeholder():
    assert frame_is_safe("好問題，{Q}，我查查。") is True
    assert frame_is_safe("好問題，沒有佔位符。") is False
    assert frame_is_safe("{Q} 還有 {Q} 兩個。") is False


def test_frame_rejects_fact_in_shell():
    """框架本身（{Q} 以外）不可有數字/英文——LLM 別偷塞事實。"""
    assert frame_is_safe("我猜是Verstappen，{Q}，查一下") is False
    assert frame_is_safe("好問題，{Q}，1976年對吧") is False


def test_frame_rejects_overlong_shell():
    assert frame_is_safe("好問題" * 20 + "{Q}") is False


# ── build_cover：套主體 ──────────────────────────────────────────────────────

def test_build_inserts_subject():
    out = build_cover("好問題，{Q}，讓我查一下。", "蕭煌奇是什麼時候出生")
    assert out == "好問題，蕭煌奇是什麼時候出生，讓我查一下。"


def test_build_rejects_bad_frame():
    assert build_cover("沒有佔位符", "主體") is None
    assert build_cover("好問題，{Q}", "") is None


# ── 核心安全性：主體永遠是原句、LLM 碰不到答案 ──────────────────────────────

def test_fabricated_name_impossible_by_construction():
    """即使 LLM 想掰『蕭亞斯』——它只能出框架，主體由我們套原句，掰名無處可放。"""
    q = "F1誰是最年輕獲得分站冠軍的"
    # LLM 回一個正常框架（它根本不知道答案是誰）
    out = build_cover("好問題，{Q}，讓我查查。", extract_subject(q))
    assert "蕭亞斯" not in out
    assert "F1誰是最年輕獲得分站冠軍的" in out


# ── 安全 fallback ────────────────────────────────────────────────────────────

def test_fallback_contains_subject_and_placeholder_filled():
    fb = safe_fallback_cover("怎麼去蘭嶼")
    assert "蘭嶼" in fb and "{Q}" not in fb


# ── generate_cover ───────────────────────────────────────────────────────────

def test_generate_uses_llm_frame():
    async def good(system, q):
        return "這個嘛，{Q}，讓我查一下。"
    out = asyncio.run(generate_cover("幫我查蕭煌奇是什麼時候出生", good))
    assert "蕭煌奇是什麼時候出生" in out and "這個嘛" in out


def test_generate_falls_back_on_frame_with_fact():
    """LLM 在框架塞事實 → 框架不合格 → 退靜態框架（仍套原句主體）。"""
    async def leaky(system, q):
        return "我猜是Verstappen，{Q}，查一下"
    out = asyncio.run(generate_cover("F1誰是最年輕獲得分站冠軍的", leaky))
    assert "verstappen" not in out.lower()
    assert "F1誰是最年輕獲得分站冠軍的" in out


def test_generate_falls_back_on_missing_placeholder():
    async def noplace(system, q):
        return "好問題，讓我查一下答案是周杰倫。"
    out = asyncio.run(generate_cover("查蕭煌奇生日", noplace))
    assert "周杰倫" not in out


def test_generate_falls_back_on_none():
    async def dead(system, q):
        return None
    out = asyncio.run(generate_cover("怎麼去蘭嶼啊", dead))
    assert "{Q}" not in out and out


def test_generate_falls_back_on_exception():
    async def boom(system, q):
        raise RuntimeError("429")
    out = asyncio.run(generate_cover("怎麼去蘭嶼啊", boom))
    assert "{Q}" not in out and out
