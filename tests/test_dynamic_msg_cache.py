"""TDD — generate_dynamic_system_msg 降載快取（純評語池輪播 + DJ 按 context 快取）。"""
from __future__ import annotations

import json

from dynamic_msg_cache import DynamicMsgCache, parse_quips


# ── parse_quips ───────────────────────────────────────────────────────────

def test_parse_quips_strips_numbering_and_quotes():
    raw = '1. 「人生好難」\n2) 宇宙真冷\n- 別煩我\n3、又是無意義的一天'
    out = parse_quips(raw)
    assert out == ["人生好難", "宇宙真冷", "別煩我", "又是無意義的一天"]


def test_parse_quips_dedups_and_filters_short():
    assert parse_quips("嗯\n嗯\n人生好難\n人生好難\n宇宙真冷") == ["人生好難", "宇宙真冷"]


def test_parse_quips_too_few_returns_empty():
    assert parse_quips("只有一句") == []   # <2 → caller 退回單句
    assert parse_quips("") == []


# ── 純評語池 ───────────────────────────────────────────────────────────────

def _clock():
    t = [1000.0]
    return t, (lambda: t[0])


def _fixed_rng(idx=0):
    class _R:
        def choice(self, seq):
            return seq[idx % len(seq)]
    return _R()


def test_quip_pool_round_robin_no_llm(tmp_path):
    c = DynamicMsgCache(str(tmp_path / "c.json"), rng=_fixed_rng(1))
    assert c.get_quip("joke_request") is None          # 空 → miss（caller 要生）
    c.set_quips("joke_request", ["甲", "乙", "丙"])
    assert c.get_quip("joke_request") == "乙"           # 命中 → 無 LLM


def test_quip_pool_expires_after_ttl(tmp_path):
    t, now = _clock()
    c = DynamicMsgCache(str(tmp_path / "c.json"), now=now, rng=_fixed_rng(0))
    c.set_quips("cooldown", ["等吧", "漫長"])
    assert c.get_quip("cooldown") == "等吧"
    t[0] += 7 * 86400 + 1                               # 過 7 天
    assert c.get_quip("cooldown") is None               # 過期 → 重生


def test_quip_set_ignores_empty(tmp_path):
    c = DynamicMsgCache(str(tmp_path / "c.json"))
    c.set_quips("joke_request", ["", "  ", None])       # 全空
    assert c.get_quip("joke_request") is None


# ── DJ 按 context 快取 ──────────────────────────────────────────────────────

def test_dj_cache_same_context_reused(tmp_path):
    c = DynamicMsgCache(str(tmp_path / "c.json"))
    ctx = "周杰倫 七里香 2004"
    assert c.get_dj("stream_now_playing", ctx) is None
    c.set_dj("stream_now_playing", ctx, "這首七里香...")
    assert c.get_dj("stream_now_playing", ctx) == "這首七里香..."   # 同首歌重播重用


def test_dj_cache_different_context_misses(tmp_path):
    c = DynamicMsgCache(str(tmp_path / "c.json"))
    c.set_dj("stream_now_playing", "周杰倫 七里香", "intro A")
    assert c.get_dj("stream_now_playing", "陶喆 流沙") is None       # 不同歌 → miss


def test_dj_cache_expires_after_ttl(tmp_path):
    t, now = _clock()
    c = DynamicMsgCache(str(tmp_path / "c.json"), now=now)
    c.set_dj("dj_interjection", "ctx", "text")
    assert c.get_dj("dj_interjection", "ctx") == "text"
    t[0] += 30 * 86400 + 1
    assert c.get_dj("dj_interjection", "ctx") is None


# ── 持久化（撐過重啟）──────────────────────────────────────────────────────

def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "c.json")
    c1 = DynamicMsgCache(p, rng=_fixed_rng(0))
    c1.set_quips("joke_request", ["甲", "乙"])
    c1.set_dj("stream_now_playing", "ctx", "intro")
    c2 = DynamicMsgCache(p, rng=_fixed_rng(0))           # 新實例（模擬重啟）讀同檔
    assert c2.get_quip("joke_request") == "甲"
    assert c2.get_dj("stream_now_playing", "ctx") == "intro"


def test_corrupt_file_fails_open(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    c = DynamicMsgCache(str(p))                          # 壞檔 → 當空快取、不 crash
    assert c.get_quip("x") is None
