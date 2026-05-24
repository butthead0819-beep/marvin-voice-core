"""TDD — Plan C9: 每次 LLM dispatch 寫一筆 jsonl 進 records/llm_routing.jsonl.

C10 上線後 1 週 observation 的資料源。內容：
{ts, route, purpose, speaker, provider, model, latency_ms, tokens, success, error}

Bus path: route="bus", winner_provider / winner_model 從 LLMBus.last_dispatch 讀.
Legacy path: route="legacy" — Phase 2 不細寫 provider attribution (太侵入 902 行),
            僅記 success / latency 給 baseline 比較.

要求：metrics 寫檔失敗（disk full / perm 錯）**不該** 拋 — dispatch 不能因 log 失敗壞掉.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# 1. log_dispatch 寫 jsonl 格式正確
# ---------------------------------------------------------------------------

def test_log_dispatch_writes_jsonl_entry(tmp_path, monkeypatch):
    from llm_agents import metrics
    log_path = tmp_path / "llm_routing.jsonl"
    monkeypatch.setattr(metrics, "_LOG_PATH", log_path)

    metrics.log_dispatch(
        route="bus", purpose="cleaner", speaker="alice",
        provider="groq", model="llama-3.1-8b-instant",
        latency_ms=420, tokens=85, success=True,
    )
    entries = _read_jsonl(log_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["route"] == "bus"
    assert e["purpose"] == "cleaner"
    assert e["speaker"] == "alice"
    assert e["provider"] == "groq"
    assert e["model"] == "llama-3.1-8b-instant"
    assert e["latency_ms"] == 420
    assert e["tokens"] == 85
    assert e["success"] is True
    assert "ts" in e


def test_log_dispatch_appends_multiple_entries(tmp_path, monkeypatch):
    from llm_agents import metrics
    log_path = tmp_path / "llm_routing.jsonl"
    monkeypatch.setattr(metrics, "_LOG_PATH", log_path)
    for i in range(5):
        metrics.log_dispatch(route="bus", purpose="cleaner", speaker=None,
                             provider="groq", model="m", latency_ms=100, tokens=10, success=True)
    assert len(_read_jsonl(log_path)) == 5


def test_log_dispatch_failure_returns_silently(tmp_path, monkeypatch):
    """metrics 寫失敗不該拋 — dispatch 流程不能因 log fail 壞掉。"""
    from llm_agents import metrics
    bad_path = tmp_path / "non_existent_subdir" / "..deep..probably_unwritable" / "x.jsonl"
    monkeypatch.setattr(metrics, "_LOG_PATH", bad_path)

    # 故意 patch mkdir 拋例外
    def broken_mkdir(*a, **kw):
        raise PermissionError("test")
    monkeypatch.setattr(Path, "mkdir", broken_mkdir)

    # 不該拋
    metrics.log_dispatch(route="bus", purpose="cleaner", speaker=None,
                         provider="groq", model="m", latency_ms=100, tokens=10, success=True)


# ---------------------------------------------------------------------------
# 2. LLMBus.last_dispatch metadata 該記錄 winner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bus_records_last_dispatch_metadata():
    from llm_agents.base import LLMAgent, LLMBid, LLMBus, LLMContext

    a = MagicMock(spec=LLMAgent)
    a.name = "a"
    a.priority = 50
    a.purpose_compatible = frozenset()
    a.bid = MagicMock(return_value=LLMBid(0.7, "groq", "llama-x", 300, 10, "happy"))
    a.handle = AsyncMock(return_value="response")

    bus = LLMBus([a])
    assert getattr(bus, "last_dispatch", None) is None  # 還沒 dispatch
    await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    meta = bus.last_dispatch
    assert meta is not None
    assert meta.winner_provider == "groq"
    assert meta.winner_model == "llama-x"


# ---------------------------------------------------------------------------
# 3. _call_llm bus path 寫 metrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_llm_writes_bus_metrics_on_success(tmp_path, monkeypatch):
    from llm_agents import metrics
    from llm_agents.base import LLMBid, LLMBus, LLMContext, LLMAgent

    log_path = tmp_path / "llm_routing.jsonl"
    monkeypatch.setattr(metrics, "_LOG_PATH", log_path)
    monkeypatch.setenv("LLM_BUS", "true")

    from gemini_router_llm import GeminiRouterLLMMixin
    obj = GeminiRouterLLMMixin.__new__(GeminiRouterLLMMixin)
    obj.dna = {"helpfulness": 3}
    obj.prompt_manager = MagicMock()
    obj.vision_enabled = False
    obj.memory = MagicMock()

    # 構 minimal bus
    a = MagicMock(spec=LLMAgent)
    a.name = "groq"
    a.priority = 10
    a.purpose_compatible = frozenset()
    a.bid = MagicMock(return_value=LLMBid(0.7, "groq", "llama-3.1-8b-instant", 400, 50, "happy"))
    a.handle = AsyncMock(return_value="bus result")
    obj._llm_bus = LLMBus([a])

    await obj._call_llm("sys", "user", tier="simple", speaker="bob")

    entries = _read_jsonl(log_path)
    assert len(entries) == 1, f"預期 1 條 metrics, 實際: {len(entries)}"
    e = entries[0]
    assert e["route"] == "bus"
    assert e["success"] is True
    assert e["provider"] == "groq"
    assert e["model"] == "llama-3.1-8b-instant"
    assert e["speaker"] == "bob"
    assert e["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_call_llm_writes_bus_metrics_on_no_llm_available(tmp_path, monkeypatch):
    from llm_agents import metrics
    from llm_agents.base import LLMBid, LLMBus, LLMContext, LLMAgent, NoLLMAvailable

    log_path = tmp_path / "llm_routing.jsonl"
    monkeypatch.setattr(metrics, "_LOG_PATH", log_path)
    monkeypatch.setenv("LLM_BUS", "true")

    from gemini_router_llm import GeminiRouterLLMMixin
    obj = GeminiRouterLLMMixin.__new__(GeminiRouterLLMMixin)
    obj.dna = {"helpfulness": 3}
    obj.prompt_manager = MagicMock()
    obj.vision_enabled = False
    obj.memory = MagicMock()

    a = MagicMock(spec=LLMAgent)
    a.name = "groq"
    a.priority = 10
    a.purpose_compatible = frozenset()
    a.bid = MagicMock(return_value=LLMBid(0.0, "groq", "m", 0, 0, "cooldown"))
    a.handle = AsyncMock()
    obj._llm_bus = LLMBus([a])

    result = await obj._call_llm("sys", "user", speaker="bob")
    assert result == ""
    entries = _read_jsonl(log_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["route"] == "bus"
    assert e["success"] is False
    assert "no_llm_available" in e["error"].lower() or "cooldown" in e["error"].lower()
