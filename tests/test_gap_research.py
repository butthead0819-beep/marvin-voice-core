"""gap_research — 資訊真空偵測（功能 1+2 Phase 1，shadow）。

事件驅動：每句 STT utterance 先過廉價 pre-gate（純規則、無 LLM），命中才跑
UncertaintyDetector（cheap LLM）。pre-gate 高 recall 粗篩 + cooldown 限頻，
精準度交給 LLM。本檔測 pure core；live voice_controller 串接另行處理。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gap_research import (
    ResearchRequest,
    UncertaintyDetector,
    append_record,
    build_record,
    has_uncertainty_signal,
    parse_detection,
    resolve_mode,
    should_escalate,
)


# ── pre-gate：不確定訊號（純規則）──────────────────────────────────────────────

def test_signal_detects_question_and_lexicon():
    assert has_uncertainty_signal("不知道 M4 Pro 跑不跑得動 72B 模式？")
    assert has_uncertainty_signal("上個月我們到底跟 A 供應商談到什麼價錢")


def test_signal_false_on_flat_chitchat():
    assert not has_uncertainty_signal("倒立洗頭真的很舒服")
    assert not has_uncertainty_signal("我喜歡這首歌")


def test_signal_false_on_empty():
    assert not has_uncertainty_signal("")
    assert not has_uncertainty_signal("   ")


# ── pre-gate：cooldown 限頻 ───────────────────────────────────────────────────

def test_should_escalate_requires_signal():
    """無訊號 → 不升級，不管 cooldown。"""
    assert should_escalate("天氣真好", last_fire_ts=0.0, now=9999.0, cooldown_s=60) is False


def test_should_escalate_blocks_within_cooldown():
    """有訊號但距上次太近 → 擋（同一波疑惑只查一次）。"""
    assert should_escalate("到底是多少？", last_fire_ts=100.0, now=130.0, cooldown_s=60) is False


def test_should_escalate_allows_after_cooldown():
    assert should_escalate("到底是多少？", last_fire_ts=100.0, now=200.0, cooldown_s=60) is True


def test_should_escalate_allows_when_never_fired():
    """last_fire_ts=None（從沒觸發過）+ 有訊號 → 升級。"""
    assert should_escalate("為什麼會這樣？", last_fire_ts=None, now=50.0, cooldown_s=60) is True


# ── 偵測結果解析（純函式）─────────────────────────────────────────────────────

def test_parse_none_returns_none():
    assert parse_detection("NONE", snippet="x") is None
    assert parse_detection("  none  ", snippet="x") is None


def test_parse_query_returns_request():
    req = parse_detection("QUERY: M4 Pro 能否跑 72B 模型", snippet="原文片段")
    assert isinstance(req, ResearchRequest)
    assert req.query == "M4 Pro 能否跑 72B 模型"
    assert req.snippet == "原文片段"


def test_parse_conservative_on_garbage():
    """非 NONE 也非 QUERY: 前綴 → 保守回 None（不對垃圾貿然行動）。"""
    assert parse_detection("我覺得啦大概", snippet="x") is None
    assert parse_detection("", snippet="x") is None


def test_parse_empty_query_after_prefix_returns_none():
    assert parse_detection("QUERY:   ", snippet="x") is None


# ── mode resolver：off/shadow/live，預設安全 off ──────────────────────────────

def test_resolve_mode_maps_values():
    assert resolve_mode("shadow") == "shadow"
    assert resolve_mode("live") == "live"
    assert resolve_mode("off") == "off"
    assert resolve_mode("SHADOW") == "shadow"  # 大小寫不敏感


def test_resolve_mode_defaults_off_on_missing_or_unknown():
    assert resolve_mode(None) == "off"
    assert resolve_mode("") == "off"
    assert resolve_mode("garbage") == "off"  # 未知 → 安全 off


# ── UncertaintyDetector：注入 LLM，async ──────────────────────────────────────

@pytest.mark.asyncio
async def test_detect_returns_request_on_gap():
    async def fake_llm(prompt: str) -> str:
        assert "滾動緩衝內容" in prompt  # buffer 有進 prompt
        return "QUERY: 帳篷抗風數據"

    det = UncertaintyDetector(llm=fake_llm)
    req = await det.detect("滾動緩衝內容：他們在聊帳篷抗風")
    assert req.query == "帳篷抗風數據"


@pytest.mark.asyncio
async def test_detect_returns_none_when_no_gap():
    async def fake_llm(prompt: str) -> str:
        return "NONE"

    det = UncertaintyDetector(llm=fake_llm)
    assert await det.detect("純閒聊") is None


# ── shadow 記錄（量誤報率的底料）──────────────────────────────────────────────

def test_build_record_with_request():
    rec = build_record(
        mode="shadow", snippet="他們在聊帳篷",
        request=ResearchRequest(query="帳篷抗風數據", snippet="他們在聊帳篷"),
        now=123.0,
    )
    assert rec["mode"] == "shadow"
    assert rec["query"] == "帳篷抗風數據"
    assert rec["snippet"] == "他們在聊帳篷"
    assert rec["delivered"] is False  # Phase 1 不交付
    assert rec["ts"] == 123.0


def test_build_record_without_request_logs_negative():
    """gate 過了但 LLM 判 NONE → 也記錄（負樣本，算誤報率要用）。"""
    rec = build_record(mode="shadow", snippet="閒聊", request=None, now=1.0)
    assert rec["query"] is None
    assert rec["delivered"] is False


def test_append_record_writes_jsonl(tmp_path: Path):
    p = tmp_path / "gap_research.jsonl"
    append_record(p, {"ts": 1.0, "mode": "shadow", "query": "x"})
    append_record(p, {"ts": 2.0, "mode": "shadow", "query": None})
    lines = p.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["query"] == "x"
    assert json.loads(lines[1])["query"] is None
