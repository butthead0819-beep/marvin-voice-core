"""Gemini 模型 ID 與搜尋計費設定（2026-10-23 對照官方計價頁）。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_cleaner_model_is_ga_flash_lite():
    # preview 版不在官方價目表上；主聊天／DJ 付費口白／STT 冷啟動共用這個設定
    src = (ROOT / "gemini_router.py").read_text(encoding="utf-8")
    assert 'self.cleaner_model = "gemini-3.1-flash-lite"' in src
    assert "gemini-3.1-flash-lite-preview" not in src


def test_grounding_search_not_charged_within_free_quota():
    # Gemini 3+ 每月前 5000 次搜尋免費（共用），Marvin 月用量遠低於此
    from intent_agents import grounded_qa_agent as gqa
    assert gqa.GROUNDING_USD_PER_SEARCH == 0.0
