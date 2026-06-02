"""
daily review 分析改走 LLM bus（analyze tier），取代 hardcode genai.Client +
REVIEW_MODEL。

2026-06-02：model 到處寫死的反模式第 3 次炸（Cerebras 404 / flash-preview 404 /
pro 配額爆）。daily review 自開 genai.Client 繞過 bus → 一過期就獨立炸、bus 的
fallback/cooldown/timeout 幫不上。改走 router.analyze：model 集中 llm_pool
ProviderSpec 一處管，跨 provider fallback。

純函式測 _call_review_llm_async（注入 fake router）；不打真 LLM。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
import pytest


def _mod():
    name = "scripts.analyze_daily_log"
    if name in sys.modules:
        del sys.modules[name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(name)


def _fake_paid_call(returns):
    """模擬 llm_pool.call_paid_review（async，回 content 字串或 None）。"""
    seq = returns if isinstance(returns, list) else [returns]
    calls = {"n": 0, "kwargs": []}

    async def _paid_call(content, **kwargs):
        calls["kwargs"].append(kwargs)
        r = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return r

    _paid_call.calls = calls
    return _paid_call


# ── 1. happy path：回有效 JSON → 解析成 dict，帶 system ─────────────────────

def test_delegates_to_paid_call_with_system():
    m = _mod()
    payload = {"players": {}, "_meta": {"review_date": "2026-06-02"}}
    paid = _fake_paid_call(json.dumps(payload, ensure_ascii=False))

    result = m.call_review_llm("分析這批 log", paid_call=paid)

    assert result == payload
    assert paid.calls["n"] == 1
    assert paid.calls["kwargs"][0].get("system")   # 帶 system prompt


# ── 2. ```json fence 被 strip ────────────────────────────────────────────────

def test_strips_code_fences():
    m = _mod()
    paid = _fake_paid_call('```json\n{"players": {}}\n```')
    assert m.call_review_llm("x", paid_call=paid) == {"players": {}}


# ── 3. paid_call 回 None（全 model 失敗）→ raise ────────────────────────────

def test_raises_when_all_models_fail():
    m = _mod()
    paid = _fake_paid_call(None)
    with pytest.raises(Exception):
        m.call_review_llm("x", paid_call=paid)


# ── 4. 第一次截斷 JSON → 重試精簡版 → 第二次成功 ───────────────────────────

def test_retry_on_truncated_json():
    m = _mod()
    good = {"players": {}, "_meta": {}}
    paid = _fake_paid_call(['{"players": {"a": ', json.dumps(good)])
    result = m.call_review_llm("x", paid_call=paid)
    assert result == good
    assert paid.calls["n"] == 2  # 重試了一次


# ── 5. model 集中在 llm_pool（不在 analyze_daily_log 寫死）─────────────────

def test_models_centralized_in_llm_pool():
    import importlib, sys as _sys
    from pathlib import Path
    base = Path(__file__).parent.parent
    if str(base) not in _sys.path:
        _sys.path.insert(0, str(base))
    llm_pool = importlib.import_module("llm_pool")
    assert hasattr(llm_pool, "_PAID_REVIEW_MODELS")
    assert any("gemini" in mdl for mdl in llm_pool._PAID_REVIEW_MODELS)
    # analyze_daily_log 不該再有自己的 model fallback 列表
    m = _mod()
    assert not hasattr(m, "review_model_fallbacks")
