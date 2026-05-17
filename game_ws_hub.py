"""GameWSHub — 瀏覽器用 WebSocket server，服務 Busted99 遊戲 UI。

設計原則：
- 不持有任何遊戲邏輯，只做廣播 + 動作轉發
- 新 client 連上立刻送最後一筆 game_state（snapshot）
- action_handler callback 負責解讀動作（由 cog 注入）
- port 預設 8767，不與 CompanionBridge(8766) 衝突
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from aiohttp import web, WSMsgType

logger = logging.getLogger("GameWSHub")


class GameWSHub:
    def __init__(
        self,
        port: int = 8767,
        host: str = "0.0.0.0",
        action_handler: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self._port = port
        self._host = host
        self._action_handler = action_handler
        self._clients: set[web.WebSocketResponse] = set()
        self._clients_lock = asyncio.Lock()
        self._last_state: dict[str, Any] | None = None
        self._runner: web.AppRunner | None = None
        # token → user_id resolver，由 cog 注入
        self._token_resolver: Callable[[str], str | None] | None = None

    def set_token_resolver(self, fn: Callable[[str], str | None]) -> None:
        """注入 token 解析函數（通常是 cog.resolve_token）。"""
        self._token_resolver = fn

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/game-ws", self._handle_ws)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/busted.html", self._handle_busted_index)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
            logger.info(f"[GameWSHub] listening on {self._host}:{self._port}")
        except OSError as e:
            logger.warning(f"[GameWSHub] 無法綁定 {self._host}:{self._port}: {e}")
            self._runner = None

    async def stop(self) -> None:
        async with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for ws in clients:
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def is_running(self) -> bool:
        return self._runner is not None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ── Broadcast ─────────────────────────────────────────────────────────────

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """廣播 JSON 給所有連線 client，同時更新 snapshot。"""
        if payload.get("type") == "game_state":
            self._last_state = payload
        text = json.dumps(payload, ensure_ascii=False)
        # Snapshot under lock, then send OUTSIDE lock — prevents blocking new
        # connections while waiting for slow/dead clients to complete send.
        async with self._clients_lock:
            snapshot = list(self._clients)
        dead: list[web.WebSocketResponse] = []
        for ws in snapshot:
            try:
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)

    # ── WebSocket handler ─────────────────────────────────────────────────────

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        async with self._clients_lock:
            self._clients.add(ws)

        # 新 client 立刻送 snapshot
        if self._last_state is not None:
            try:
                await ws.send_str(json.dumps(self._last_state, ensure_ascii=False))
            except Exception:
                pass

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            async with self._clients_lock:
                self._clients.discard(ws)

        return ws

    async def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"[GameWSHub] 非 JSON 訊息: {raw[:80]}")
            return
        if not isinstance(data, dict) or "type" not in data:
            return
        # Strip any client-supplied resolved_user_id; only the server-side token
        # resolver is allowed to populate this field. Without this, a client can
        # impersonate any player by sending {"resolved_user_id": "<victim_id>"}
        # with no token at all.
        data.pop("resolved_user_id", None)
        token = data.get("token")
        if token and self._token_resolver is not None:
            resolved = self._token_resolver(token)
            if resolved:
                data["resolved_user_id"] = resolved
        if self._action_handler is not None:
            try:
                await self._action_handler(data)
            except Exception as e:
                logger.warning(f"[GameWSHub] action_handler 失敗: {e}")

    async def _handle_index(self, request: web.Request) -> web.Response:
        """直接從 assets/busted99.html 回傳遊戲頁面。"""
        import os
        html_path = os.path.join(os.path.dirname(__file__), "assets", "busted99.html")
        if not os.path.exists(html_path):
            return web.Response(text="<h1>busted99.html not found</h1>", content_type="text/html")
        with open(html_path, encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")

    async def _handle_busted_index(self, request: web.Request) -> web.Response:
        """服務 Busted 遊戲頁面（assets/busted.html）。"""
        import os
        html_path = os.path.join(os.path.dirname(__file__), "assets", "busted.html")
        if not os.path.exists(html_path):
            return web.Response(text="<h1>busted.html not found</h1>", content_type="text/html")
        with open(html_path, encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")
