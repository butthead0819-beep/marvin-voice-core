"""TDD — GameWSHub：瀏覽器用 WebSocket 接遊戲狀態 + 送動作

驗項：
A) GameWSHub 可啟動、可停止（無 exception）
B) broadcast() 把 dict 以 JSON 送給所有連線 client
C) 新 client 連上時立刻收到最後一筆 game_state（snapshot）
D) 收到 b99_guess action 後呼叫 action_handler
E) 收到未知 type 的訊息不 crash
F) client 斷線後不留在 registry，broadcast 不爆
"""

from __future__ import annotations

import asyncio
import json
import pytest
import aiohttp


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_hub(**kwargs):
    from game_ws_hub import GameWSHub
    return GameWSHub(**kwargs)


async def _connect(port: int, path: str = "/game-ws"):
    """開啟一條 WebSocket 連線，回傳 (session, ws)。"""
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(f"http://127.0.0.1:{port}{path}")
    return session, ws


# ── A: lifecycle ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hub_starts_and_stops():
    hub = _make_hub(port=18767)
    await hub.start()
    assert hub.is_running
    await hub.stop()
    assert not hub.is_running


# ── B: broadcast 送達所有 client ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_broadcast_reaches_all_clients():
    hub = _make_hub(port=18768)
    await hub.start()

    s1, ws1 = await _connect(18768)
    s2, ws2 = await _connect(18768)
    await asyncio.sleep(0.05)  # 等連線 register

    payload = {"type": "game_state", "phase": "guessing", "guesser": "狗與露"}
    await hub.broadcast(payload)

    msg1 = await asyncio.wait_for(ws1.receive(), timeout=2.0)
    msg2 = await asyncio.wait_for(ws2.receive(), timeout=2.0)

    data1 = json.loads(msg1.data)
    data2 = json.loads(msg2.data)

    assert data1["phase"] == "guessing"
    assert data2["guesser"] == "狗與露"

    await ws1.close(); await s1.close()
    await ws2.close(); await s2.close()
    await hub.stop()


# ── C: 新 client 連上立刻收 snapshot ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_client_receives_snapshot_on_connect():
    hub = _make_hub(port=18769)
    await hub.start()

    # 先廣播一筆 state
    await hub.broadcast({"type": "game_state", "phase": "joining", "players": []})
    await asyncio.sleep(0.05)

    # 之後連上的 client 也要立刻收到
    s, ws = await _connect(18769)
    msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
    data = json.loads(msg.data)

    assert data["type"] == "game_state"
    assert data["phase"] == "joining"

    await ws.close(); await s.close()
    await hub.stop()


# ── D: action_handler 被呼叫 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_action_handler_called_on_incoming_action():
    received = []

    async def handler(action: dict):
        received.append(action)

    hub = _make_hub(port=18770, action_handler=handler)
    await hub.start()

    s, ws = await _connect(18770)
    await asyncio.sleep(0.05)

    await ws.send_str(json.dumps({"type": "b99_guess", "name": "狗與露", "number": 42}))
    await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0]["type"] == "b99_guess"
    assert received[0]["number"] == 42

    await ws.close(); await s.close()
    await hub.stop()


# ── E: 未知 type 不 crash ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_action_type_does_not_crash():
    hub = _make_hub(port=18771)
    await hub.start()

    s, ws = await _connect(18771)
    await asyncio.sleep(0.05)

    await ws.send_str(json.dumps({"type": "totally_unknown", "data": "xyz"}))
    await asyncio.sleep(0.1)

    # hub should still be running
    assert hub.is_running

    await ws.close(); await s.close()
    await hub.stop()


# ── F: 斷線 client 不留在 registry ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_disconnected_client_removed_from_registry():
    hub = _make_hub(port=18772)
    await hub.start()

    s, ws = await _connect(18772)
    await asyncio.sleep(0.05)
    assert hub.client_count == 1

    await ws.close(); await s.close()
    await asyncio.sleep(0.1)

    assert hub.client_count == 0

    # broadcast to empty registry should not raise
    await hub.broadcast({"type": "game_state", "phase": "joining"})

    await hub.stop()
