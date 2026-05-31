"""marmo_server T9：feature-flag-gated 改線到 IntentBus。

env MARMO_DUAL_SPEAK 控制：
  - 預設 / "false" → 走既有 `vc.play_tts` 路徑（不改現有行為，現有 test 不破）
  - "true"        → 構造 IntentContext(dispatch_source="marmo_inject", payload={text, job_id})
                    asyncio.create_task(bus.dispatch(ctx))；不 await（fire-and-forget D2 決定）

驗證：
  - flag off → play_tts 被呼叫、bus.dispatch 不被呼叫
  - flag on + bus 可用 → bus.dispatch 被呼叫 ctx 帶 marmo_inject + payload；play_tts 不被呼叫
  - flag on + bus 不可用（vc._intent_bus 是 None / 沒這 attr） → fallback 走 play_tts（resilient）
  - flag on + 既有 gates 仍守住：empty text 400、duplicate job_id 不 dispatch 第二次、game_mode drop
  - HTTP 立刻回 200（fire-and-forget 不 await）
"""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web


def _reload_marmo_server():
    import marvin_voice_core.marmo_server as ms
    importlib.reload(ms)
    return ms


@pytest.fixture
def vc_with_bus():
    """Mock VC 同時帶 _intent_bus（mock async dispatch）。"""
    vc = MagicMock()
    vc.play_tts = AsyncMock()
    vc.game_mode = False
    vc.stream_mode = False  # 顯式 False，避免 MagicMock 自動產生 truthy 子 mock
    vc._intent_bus = MagicMock()
    vc._intent_bus.dispatch = AsyncMock(return_value=None)
    return vc


@pytest.fixture
def vc_without_bus():
    """Mock VC 沒有 _intent_bus (None) → 模擬 bus 不可用情境。"""
    vc = MagicMock()
    vc.play_tts = AsyncMock()
    vc.game_mode = False
    vc.stream_mode = False
    vc._intent_bus = None
    return vc


async def _make_client(vc, aiohttp_client, monkeypatch, flag_value=None):
    if flag_value is None:
        monkeypatch.delenv("MARMO_DUAL_SPEAK", raising=False)
    else:
        monkeypatch.setenv("MARMO_DUAL_SPEAK", flag_value)
    monkeypatch.delenv("MARMO_TOKEN", raising=False)
    ms = _reload_marmo_server()
    server = ms.MarmoServer(voice_controller=vc)
    app = web.Application()
    app.router.add_post("/marmo-result", server._handle_result)
    return await aiohttp_client(app), server


# ── Flag OFF → existing behavior unchanged ────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_off_uses_play_tts_not_dispatch(aiohttp_client, vc_with_bus, monkeypatch):
    c, _ = await _make_client(vc_with_bus, aiohttp_client, monkeypatch, flag_value=None)
    resp = await c.post("/marmo-result", json={"text": "hello", "job_id": "j1"})
    assert resp.status == 200
    await asyncio.sleep(0)
    vc_with_bus.play_tts.assert_awaited_once()
    vc_with_bus._intent_bus.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_flag_explicit_false_uses_play_tts(aiohttp_client, vc_with_bus, monkeypatch):
    c, _ = await _make_client(vc_with_bus, aiohttp_client, monkeypatch, flag_value="false")
    await c.post("/marmo-result", json={"text": "x", "job_id": "j"})
    await asyncio.sleep(0)
    vc_with_bus.play_tts.assert_awaited_once()
    vc_with_bus._intent_bus.dispatch.assert_not_called()


# ── Flag ON → dispatch via IntentBus ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_on_calls_bus_dispatch_with_correct_ctx(aiohttp_client, vc_with_bus, monkeypatch):
    c, _ = await _make_client(vc_with_bus, aiohttp_client, monkeypatch, flag_value="true")
    resp = await c.post("/marmo-result", json={"text": "找到了第 7083 行", "job_id": "j-T9"})
    assert resp.status == 200
    # Fire-and-forget dispatch needs a yield to scheduler
    await asyncio.sleep(0.01)
    vc_with_bus.play_tts.assert_not_called()
    vc_with_bus._intent_bus.dispatch.assert_awaited_once()

    ctx = vc_with_bus._intent_bus.dispatch.call_args.args[0]
    assert ctx.dispatch_source == "marmo_inject"
    assert ctx.payload["text"] == "找到了第 7083 行"
    assert ctx.payload["job_id"] == "j-T9"
    # 非 wake 來源：raw_text/query 應該存 marmo_text 方便 logging，但 wake_intent=None
    assert ctx.wake_intent is None
    # mode 預設 normal (non-game checked above)
    assert ctx.mode == "normal"


@pytest.mark.asyncio
async def test_flag_on_http_returns_fast_does_not_await_dispatch(aiohttp_client, vc_with_bus, monkeypatch):
    """HTTP 應該 fire-and-forget，不 hold connection。"""
    # 設置 dispatch 卡 0.5 秒；HTTP 應該不等
    async def _slow_dispatch(_ctx):
        await asyncio.sleep(0.5)
    vc_with_bus._intent_bus.dispatch = AsyncMock(side_effect=_slow_dispatch)

    c, _ = await _make_client(vc_with_bus, aiohttp_client, monkeypatch, flag_value="true")
    import time
    t0 = time.monotonic()
    resp = await c.post("/marmo-result", json={"text": "x", "job_id": "j"})
    elapsed = time.monotonic() - t0
    assert resp.status == 200
    assert elapsed < 0.3, f"HTTP 應立刻回 200，實際耗 {elapsed:.2f}s（dispatch 在 hold）"


# ── Flag ON + bus 不可用 → resilient fallback ─────────────────────────────────

@pytest.mark.asyncio
async def test_flag_on_bus_missing_falls_back_to_play_tts(aiohttp_client, vc_without_bus, monkeypatch):
    c, _ = await _make_client(vc_without_bus, aiohttp_client, monkeypatch, flag_value="true")
    resp = await c.post("/marmo-result", json={"text": "fallback", "job_id": "j"})
    assert resp.status == 200
    await asyncio.sleep(0)
    vc_without_bus.play_tts.assert_awaited_once()


# ── Flag ON 仍守住既有 gates ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_on_empty_text_returns_400_no_dispatch(aiohttp_client, vc_with_bus, monkeypatch):
    c, _ = await _make_client(vc_with_bus, aiohttp_client, monkeypatch, flag_value="true")
    resp = await c.post("/marmo-result", json={"text": "   "})
    assert resp.status == 400
    vc_with_bus._intent_bus.dispatch.assert_not_called()
    vc_with_bus.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_game_mode_drops_no_dispatch(aiohttp_client, vc_with_bus, monkeypatch):
    vc_with_bus.game_mode = True
    c, _ = await _make_client(vc_with_bus, aiohttp_client, monkeypatch, flag_value="true")
    resp = await c.post("/marmo-result", json={"text": "x", "job_id": "j"})
    assert resp.status == 200
    assert await resp.text() == "game_mode_active"
    vc_with_bus._intent_bus.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_duplicate_job_id_dispatches_once(aiohttp_client, vc_with_bus, monkeypatch):
    c, _ = await _make_client(vc_with_bus, aiohttp_client, monkeypatch, flag_value="true")
    r1 = await c.post("/marmo-result", json={"text": "x", "job_id": "j-dup"})
    r2 = await c.post("/marmo-result", json={"text": "x", "job_id": "j-dup"})
    assert r1.status == 200 and r2.status == 200
    assert await r2.text() == "duplicate"
    await asyncio.sleep(0.01)
    assert vc_with_bus._intent_bus.dispatch.await_count == 1
