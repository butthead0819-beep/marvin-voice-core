"""
LLM bus paid review 池：大型 batch（daily review 67k prompt）走付費 Gemini，
model 集中 llm_pool 一處管（不再 analyze_daily_log 寫死）。

2026-06-02 拍板：小/即時 call → 免費池；大型 batch → 付費 Gemini（大 context）。
免費 70b 池吃不下 67k 會卡死，故大型走 paid。但 model 管理一樣集中在 bus。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_pool import build_paid_review_pool, call_paid_review, PoolEndpoint


def _genai_resp(text="ok", in_tok=5, out_tok=5):
    """模擬 genai generate_content response（.text + .usage_metadata）。"""
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=in_tok, candidates_token_count=out_tok),
    )


def _genai_client(create_fn):
    """模擬 genai.Client：.aio.models.generate_content。"""
    c = MagicMock()
    c.aio.models.generate_content = create_fn
    return c


# ── build_paid_review_pool：Gemini model 集中、env 可前插 ────────────────────

def test_pool_uses_centralized_gemini_models():
    pool = build_paid_review_pool(env={"GOOGLE_API_KEY": "k"}, client_factory=lambda k: MagicMock())
    models = [ep.model for ep in pool.endpoints]
    assert any("gemini" in m for m in models)
    assert "gemini-2.5-flash" in models


def test_pool_env_override_goes_first():
    pool = build_paid_review_pool(env={"GOOGLE_API_KEY": "k", "MARVIN_REVIEW_MODEL": "gemini-X"},
                                  client_factory=lambda k: MagicMock())
    models = [ep.model for ep in pool.endpoints]
    assert models[0] == "gemini-X"            # env 指定優先
    assert "gemini-2.5-flash" in models       # 仍保留 fallback


def test_pool_empty_when_no_key():
    pool = build_paid_review_pool(env={})
    assert pool.endpoints == []


# ── call_paid_review：genai SDK + dispatch（fallback + cooldown）──────────────

@pytest.mark.asyncio
async def test_call_paid_review_falls_back_on_first_model_error():
    """第一個 model 失敗（404/429）→ 跳下一個。"""
    a = PoolEndpoint(name="gemini:a", model="a")
    b = PoolEndpoint(name="gemini:b", model="b")

    async def _gen(*, model, **kw):
        if model == "a":
            raise Exception("404 NOT_FOUND")
        return _genai_resp("from-b", out_tok=20)

    client = _genai_client(_gen)
    a.client = client
    b.client = client

    from llm_pool import CooldownAwarePool
    pool = CooldownAwarePool([a, b])
    out = await call_paid_review("content", system="sys", pool=pool)
    assert out == "from-b"


@pytest.mark.asyncio
async def test_call_paid_review_passes_system_and_thinking_off():
    captured = {}

    async def _gen(**kw):
        captured.update(kw)
        return _genai_resp('{"ok": true}')

    ep = PoolEndpoint(name="g", model="gemini-2.5-flash")
    ep.client = _genai_client(_gen)
    from llm_pool import CooldownAwarePool
    pool = CooldownAwarePool([ep])

    out = await call_paid_review("hello", system="SYSTEM", pool=pool)
    assert out == '{"ok": true}'
    cfg = captured["config"]
    assert cfg.system_instruction == "SYSTEM"          # system 帶入
    assert cfg.response_mime_type == "application/json"  # JSON mode
    assert cfg.thinking_config.thinking_budget == 0      # thinking 關掉（關鍵修正）
