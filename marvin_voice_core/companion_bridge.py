"""
CompanionBridge — Marvin 端的 WebSocket 雙向橋接。

設計理念：
companion-server 是另一個 process（FastAPI）。它連到本 bridge，
透過 JSON 事件交換狀態。bridge 在 Marvin 端「直接 import」
AtmosphereTracker / VectorStore / MusicMemory / SukiMemory —
這是整個 companion 設計的精髓：主專案更新了，companion 行為自動跟著走，
不需要在 bridge 重寫一份邏輯。

事件協定（兩端共用，定義在 Voice-bot-companion/companion/event_protocol.py）：
    {"type": "<event_name>", "payload": {...}, "ts": <unix_seconds>}

認證：沿用既有的 MARMO_TOKEN 環境變數（X-Marmo-Token header），
不另發明新 token。

並行：支援多個 companion-server 連線，broadcast 廣播到所有 client。
client 斷線時自動從 registry 移除。

Cog 注入（Lane F）：
    為了支援 game_force_skip_round / game_end 這類需要呼叫遊戲 cog 的事件，
    bridge 接受 `get_cog: Callable[[str], cog | None]` 參數。實務上由
    Marvin 主程式傳入 `bot.cogs.get`（或測試時的 stub）。bridge 不直接
    持有 `bot` 物件，避免循環相依。cog 介面要求：
      - DetectiveCog.force_skip_round() → coroutine
      - DetectiveCog.end_session()      → coroutine
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from aiohttp import web, WSMsgType

logger = logging.getLogger("MarvinBot.CompanionBridge")


# ── 事件 type 常數（必須與 Voice-bot-companion/companion/event_protocol.py 一致）──

# Marvin → companion（主動推送）
EVT_STT_CHUNK = "stt_chunk"
EVT_INTENT_ROUTED = "intent_routed"
EVT_TTS_STARTED = "tts_started"
EVT_TTS_DONE = "tts_done"
EVT_ATMOSPHERE_SNAPSHOT = "atmosphere_snapshot"
EVT_MEMBER_JOINED = "member_joined"
EVT_MEMBER_LEFT = "member_left"
# Lane B2：新 client 連上時推當前頻道成員（讓 UI 不必等 join 事件慢慢累積）
EVT_VOICE_CHANNEL_SNAPSHOT = "voice_channel_snapshot"
EVT_MUSIC_STARTED = "music_started"
EVT_MUSIC_ENDED = "music_ended"
EVT_MUSIC_REACTION = "music_reaction"
EVT_GAME_PHASE_CHANGED = "game_phase_changed"
EVT_GAME_ALERT = "game_alert"
EVT_MEMORY_LIST_RESPONSE = "memory_list_response"
EVT_MUSIC_RECOMMENDATIONS_RESPONSE = "music_recommendations_response"

# companion → Marvin（請求 / 控制）
EVT_ATMOSPHERE_FEEDBACK = "atmosphere_feedback"
EVT_TTS_INJECTION = "tts_injection"
EVT_MODE_CHANGE = "mode_change"
EVT_MEMORY_LIST_REQUEST = "memory_list_request"
EVT_MEMORY_DELETE = "memory_delete"
EVT_MEMORY_MARK_UNCERTAIN = "memory_mark_uncertain"
EVT_MUSIC_PLAY_REQUEST = "music_play_request"
EVT_MUSIC_SKIP = "music_skip"
EVT_MUSIC_RECOMMENDATIONS_REQUEST = "music_recommendations_request"
EVT_GAME_FORCE_SKIP_ROUND = "game_force_skip_round"
EVT_GAME_END = "game_end"
EVT_GAME_ALERT_RESPONSE = "game_alert_response"

_KNOWN_INCOMING = frozenset({
    EVT_ATMOSPHERE_FEEDBACK,
    EVT_TTS_INJECTION,
    EVT_MODE_CHANGE,
    EVT_MEMORY_LIST_REQUEST,
    EVT_MEMORY_DELETE,
    EVT_MEMORY_MARK_UNCERTAIN,
    EVT_MUSIC_PLAY_REQUEST,
    EVT_MUSIC_SKIP,
    EVT_MUSIC_RECOMMENDATIONS_REQUEST,
    EVT_GAME_FORCE_SKIP_ROUND,
    EVT_GAME_END,
    EVT_GAME_ALERT_RESPONSE,
})

# game name → cog name 的對照；外部可擴充
_GAME_TO_COG = {
    "detective": "DetectiveCog",
    "busted":    "BustedCog",
    "busted99":  "Busted99Cog",
}

_VALID_MODES = frozenset({"silent_5min", "serious", "shutup", "reset"})


def _make_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """組事件 envelope。"""
    return {"type": event_type, "payload": payload, "ts": time.time()}


class CompanionBridge:
    """
    WebSocket server，companion-server 主動連進來。

    主專案的 emitter 介面（emit_stt_chunk / emit_tts_started ...）
    由 Marvin pipeline 在關鍵節點呼叫，bridge 廣播給所有連線 client。

    入站事件（companion → Marvin）由 _handle_incoming 路由：
        - atmosphere_feedback → tracker.record_correction
        - tts_injection       → voice_controller.play_tts
        - mode_change         → 寫入 self._mode
        - memory_list_request → 合併 suki_memory + vector_store，回 memory_list_response
        - memory_delete       → vector_store.delete
        - memory_mark_uncertain → vector_store.update(doc_id, {"uncertain": True})
        - music_play_request / music_skip → 暫時 log（Lane E 接 cog）
    """

    def __init__(
        self,
        atmosphere_tracker,
        vector_store,
        music_memory,
        suki_memory,
        voice_controller=None,
        music_engine=None,
        guild_id: int = 0,
        get_cog=None,
    ):
        # 直接持有主專案物件 —— 這是 bridge 的存在意義
        self._tracker = atmosphere_tracker
        self._vector_store = vector_store
        self._music_memory = music_memory
        self._suki_memory = suki_memory
        self._voice_controller = voice_controller
        self._music_engine = music_engine
        self._guild_id = guild_id
        # cog lookup（Lane F）：實務上是 bot.cogs.get；測試時是 stub
        self._get_cog = get_cog

        # WS client registry：每條連線一個 ws 物件
        self._clients: set[web.WebSocketResponse] = set()
        self._clients_lock = asyncio.Lock()

        # aiohttp lifecycle
        self._runner: web.AppRunner | None = None

        # mode 狀態（VoiceController 可查詢）
        self._mode: str | None = None

        # 防呆雷達：alert_id → Future[bool]
        self._pending_alerts: dict[str, asyncio.Future] = {}

        # 認證 token（從環境變數讀，沿用 MARMO_TOKEN）
        self._token = os.getenv("MARMO_TOKEN", "")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, host: str = "127.0.0.1", port: int = 8766) -> None:
        app = web.Application()
        app.router.add_get("/companion-ws", self._handle_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        try:
            await site.start()
            logger.info(f"[Companion_Bridge] Listening on {host}:{port}")
        except OSError as e:
            logger.warning(f"[Companion_Bridge] 無法綁定 {host}:{port}：{e}")

    async def stop(self) -> None:
        # 主動關閉所有連線
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
    def is_connected(self) -> bool:
        return len(self._clients) > 0

    @property
    def is_running(self) -> bool:
        # bridge 是否已 start()（runner 仍有效）；給 emitter hook 當門檻用
        return self._runner is not None

    # ── WS 連線 handler ───────────────────────────────────────────────────────

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        # 認證：MARMO_TOKEN 有設時必須匹配
        if self._token and request.headers.get("X-Marmo-Token") != self._token:
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async with self._clients_lock:
            self._clients.add(ws)
        logger.info(f"[Companion_Bridge] client connected（目前 {len(self._clients)} 條）")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_incoming(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning(f"[Companion_Bridge] ws error: {ws.exception()}")
                    break
        except Exception as e:
            logger.warning(f"[Companion_Bridge] ws loop 例外: {e}")
        finally:
            async with self._clients_lock:
                self._clients.discard(ws)
            logger.info(f"[Companion_Bridge] client disconnected（目前 {len(self._clients)} 條）")

        return ws

    # ── 入站事件 dispatch ────────────────────────────────────────────────────

    async def _handle_incoming(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[Companion_Bridge] 收到非 JSON 訊息，drop")
            return

        if not isinstance(msg, dict):
            logger.warning(f"[Companion_Bridge] 訊息非 dict，drop")
            return

        event_type = msg.get("type")
        payload = msg.get("payload") or {}

        if event_type not in _KNOWN_INCOMING:
            logger.warning(f"[Companion_Bridge] 未知事件 type={event_type!r}，drop")
            return

        try:
            if event_type == EVT_ATMOSPHERE_FEEDBACK:
                self._handle_atmosphere_feedback(payload)
            elif event_type == EVT_TTS_INJECTION:
                await self._handle_tts_injection(payload)
            elif event_type == EVT_MODE_CHANGE:
                self._handle_mode_change(payload)
            elif event_type == EVT_MEMORY_LIST_REQUEST:
                await self._handle_memory_list_request(ws, payload)
            elif event_type == EVT_MEMORY_DELETE:
                self._handle_memory_delete(payload)
            elif event_type == EVT_MEMORY_MARK_UNCERTAIN:
                self._handle_memory_mark_uncertain(payload)
            elif event_type == EVT_MUSIC_PLAY_REQUEST:
                await self._handle_music_play_request(payload)
            elif event_type == EVT_MUSIC_SKIP:
                await self._handle_music_skip(payload)
            elif event_type == EVT_MUSIC_RECOMMENDATIONS_REQUEST:
                await self._handle_music_recommendations_request(ws, payload)
            elif event_type == EVT_GAME_FORCE_SKIP_ROUND:
                await self._handle_game_force_skip_round(payload)
            elif event_type == EVT_GAME_END:
                await self._handle_game_end(payload)
            elif event_type == EVT_GAME_ALERT_RESPONSE:
                self._handle_game_alert_response(payload)
        except Exception as e:
            logger.warning(f"[Companion_Bridge] handler {event_type} 失敗: {e}", exc_info=True)

    def _handle_atmosphere_feedback(self, payload: dict[str, Any]) -> None:
        snapshot_ts = payload.get("snapshot_ts")
        label = payload.get("label")
        speaker = payload.get("speaker")
        if snapshot_ts is None or label is None:
            logger.warning(f"[Companion_Bridge] atmosphere_feedback 缺欄位: {payload}")
            return
        self._tracker.record_correction(float(snapshot_ts), label, speaker)

    async def _handle_tts_injection(self, payload: dict[str, Any]) -> None:
        if self._voice_controller is None:
            logger.info("[Companion_Bridge] tts_injection 來但無 voice_controller，drop")
            return
        text = payload.get("text", "").strip()
        if not text:
            return
        voice = payload.get("voice")
        # `target` 在 protocol 規格中保留供未來個別發話使用；
        # VoiceController.play_tts 目前不支援，僅記錄不傳遞，避免 unexpected kwarg。
        target = payload.get("target")
        if target:
            logger.debug(f"[Companion_Bridge] tts_injection target={target!r} 暫不支援，忽略")
        kwargs = {}
        if voice:
            kwargs["voice"] = voice
        await self._voice_controller.play_tts(text, **kwargs)

    def _handle_mode_change(self, payload: dict[str, Any]) -> None:
        mode = payload.get("mode")
        if mode not in _VALID_MODES:
            logger.warning(f"[Companion_Bridge] mode_change 未知模式: {mode!r}")
            return
        self._mode = mode
        logger.info(f"[Companion_Bridge] mode → {mode}")

    async def _handle_memory_list_request(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        speaker = payload.get("speaker", "")
        guild_id = int(payload.get("guild_id", self._guild_id) or 0)
        limit = int(payload.get("limit", 20) or 20)

        profile: dict = {}
        try:
            profile = self._suki_memory.get_player_memory(speaker) or {}
        except Exception as e:
            logger.warning(f"[Companion_Bridge] get_player_memory 失敗: {e}")

        chunks: list = []
        try:
            chunks = self._vector_store.get_all(speaker, guild_id, limit) or []
        except Exception as e:
            logger.warning(f"[Companion_Bridge] vector_store.get_all 失敗: {e}")

        resp = _make_event(EVT_MEMORY_LIST_RESPONSE, {
            "speaker": speaker,
            "guild_id": guild_id,
            "profile": profile,
            "vector_chunks": chunks,
        })
        try:
            await ws.send_str(json.dumps(resp, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f"[Companion_Bridge] 回 memory_list_response 失敗: {e}")

    def _handle_memory_delete(self, payload: dict[str, Any]) -> None:
        doc_id = payload.get("doc_id")
        if not doc_id:
            return
        self._vector_store.delete(doc_id)

    def _handle_memory_mark_uncertain(self, payload: dict[str, Any]) -> None:
        doc_id = payload.get("doc_id")
        if not doc_id:
            return
        self._vector_store.update(doc_id, {"uncertain": True})

    # ── Lane E: Music handlers ────────────────────────────────────────────

    async def _handle_music_play_request(self, payload: dict[str, Any]) -> None:
        """收 music_play_request → 呼叫 music_engine.queue_request。

        music_engine 為 None（測試模式）時 log 並 ack；不爆。
        """
        if self._music_engine is None:
            logger.info(f"[Companion_Bridge] music_play_request 無 music_engine，ack payload={payload}")
            return
        query = payload.get("query", "")
        target = payload.get("target")
        style = payload.get("style")
        try:
            await self._music_engine.queue_request(query=query, target=target, style=style)
        except Exception as e:
            logger.warning(f"[Companion_Bridge] music_engine.queue_request 失敗: {e}")

    async def _handle_music_skip(self, payload: dict[str, Any]) -> None:
        """收 music_skip → 呼叫 music_engine.skip。"""
        if self._music_engine is None:
            logger.info(f"[Companion_Bridge] music_skip 無 music_engine，ack payload={payload}")
            return
        try:
            await self._music_engine.skip()
        except Exception as e:
            logger.warning(f"[Companion_Bridge] music_engine.skip 失敗: {e}")

    async def _handle_music_recommendations_request(
        self, ws: web.WebSocketResponse, payload: dict[str, Any]
    ) -> None:
        """收 music_recommendations_request → 查 MusicMemory，回 music_recommendations_response。

        v1 採規則式推薦：從 history 中挑近期 3 首作為 picks，reason 用模板字串。
        不呼叫 LLM；intelligence 之後再升級。
        """
        target_username = payload.get("target_username")
        target_label = target_username if target_username else "room"

        history: list[dict] = []
        user_taste: dict[str, float] = {}
        recommendations: list[dict] = []

        if self._music_memory is None:
            resp = _make_event(EVT_MUSIC_RECOMMENDATIONS_RESPONSE, {
                "target": target_label,
                "recommendations": recommendations,
                "user_taste": user_taste,
                "history": history,
            })
            try:
                await ws.send_str(json.dumps(resp, ensure_ascii=False, default=str))
            except Exception as e:
                logger.warning(f"[Companion_Bridge] 回 music_recommendations_response 失敗: {e}")
            return

        # 取 user 的點播歷史（房間級時用空 string，會吐空清單）
        username_for_query = target_username or ""
        try:
            top_songs = self._music_memory.get_top_songs_for_user(username_for_query, limit=10) or []
        except Exception as e:
            logger.warning(f"[Companion_Bridge] get_top_songs_for_user 失敗: {e}")
            top_songs = []

        # 整理 history（取最新一筆 play 的 time_slot）
        for song in top_songs:
            title = song.get("title", "")
            plays = song.get("plays", []) or []
            latest = plays[-1] if plays else {}
            reactions = song.get("reactions", {}) or {}
            user_react = reactions.get(username_for_query, {}) if username_for_query else {}
            tag = "play"
            if user_react.get("feelings"):
                tag = "love"
            history.append({
                "time": latest.get("time_slot") or latest.get("date") or "",
                "title": title,
                "tag": tag,
            })

        # 簡易 taste 計數：用 uploader 當風格 fallback；若有 style 欄位優先
        style_counts: dict[str, int] = {}
        for song in top_songs:
            style = (song.get("style") or song.get("uploader") or "其他").strip() or "其他"
            cnt = song.get("requesters", {}).get(username_for_query, 1) if username_for_query else 1
            style_counts[style] = style_counts.get(style, 0) + cnt
        total = sum(style_counts.values()) or 1
        user_taste = {k: round(v / total, 3) for k, v in style_counts.items()}

        # 推薦：取最近 3 首，reason 用模板
        for song in top_songs[:3]:
            title = song.get("title", "")
            style = song.get("style") or song.get("uploader") or "lo-fi"
            duration = song.get("duration") or 180
            if username_for_query:
                reason = f"{username_for_query} 上週聽過類似風格"
            else:
                reason = "匹配房間最近偏好"
            recommendations.append({
                "title": title,
                "style": style,
                "duration": duration,
                "reason": reason,
            })

        resp = _make_event(EVT_MUSIC_RECOMMENDATIONS_RESPONSE, {
            "target": target_label,
            "recommendations": recommendations,
            "user_taste": user_taste,
            "history": history,
        })
        try:
            await ws.send_str(json.dumps(resp, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f"[Companion_Bridge] 回 music_recommendations_response 失敗: {e}")

    # ── Lane F: Game handlers ───────────────────────────────────────────

    def _resolve_game_cog(self, game_name: str):
        """從 game name 取出對應 cog；找不到時 log 並回 None。"""
        if self._get_cog is None:
            logger.info(f"[Companion_Bridge] game 事件來但無 get_cog hook，drop game={game_name}")
            return None
        cog_name = _GAME_TO_COG.get(game_name)
        if cog_name is None:
            logger.warning(f"[Companion_Bridge] 未知 game={game_name!r}，drop")
            return None
        try:
            cog = self._get_cog(cog_name)
        except Exception as e:
            logger.warning(f"[Companion_Bridge] get_cog({cog_name}) 失敗: {e}")
            return None
        if cog is None:
            logger.info(f"[Companion_Bridge] cog {cog_name} 未註冊，drop game={game_name}")
        return cog

    async def _handle_game_force_skip_round(self, payload: dict[str, Any]) -> None:
        """收 game_force_skip_round → 呼叫 cog.force_skip_round。"""
        game_name = payload.get("game", "")
        cog = self._resolve_game_cog(game_name)
        if cog is None:
            return
        method = getattr(cog, "force_skip_round", None)
        if method is None:
            logger.info(f"[Companion_Bridge] cog 缺 force_skip_round 方法，drop game={game_name}")
            return
        try:
            await method()
        except Exception as e:
            logger.warning(f"[Companion_Bridge] cog.force_skip_round 失敗: {e}")

    def _handle_game_alert_response(self, payload: dict[str, Any]) -> None:
        """收 game_alert_response → 解析對應 future。

        payload: {alert_id, decision: "veto"|"approve"}
        """
        alert_id = payload.get("alert_id")
        decision = payload.get("decision")
        if not alert_id:
            logger.warning(f"[Companion_Bridge] game_alert_response 缺 alert_id: {payload}")
            return
        future = self._pending_alerts.pop(alert_id, None)
        if future is None:
            logger.info(f"[Companion_Bridge] game_alert_response 對應 alert_id={alert_id!r} 已過期或未知，drop")
            return
        if future.done():
            return
        approved = decision != "veto"  # 任何非 "veto" 都當 approve
        future.set_result(approved)

    async def request_radar_veto(
        self, text: str, context: dict[str, Any], timeout: float = 2.0
    ) -> bool:
        """防呆雷達：向 companion 端詢問是否攔下這段 TTS。

        - 廣播 game_alert，附 alert_id；
        - 等待 user 從 companion 端回 game_alert_response；
        - timeout 內沒回 → 視為 approve（default-safe）；
        - 無 client 連線 → 立即回 approve；
        - 任何例外 → 回 approve。

        回傳：True=放行，False=user 主動 veto。
        """
        # 無 client → 不阻塞 TTS
        if not self._clients:
            return True
        try:
            alert_id = str(uuid.uuid4())
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            self._pending_alerts[alert_id] = future

            risk = context.get("risk") or {} if isinstance(context, dict) else {}
            payload = {
                "alert_id": alert_id,
                "text": text,
                "reason": risk.get("reason", ""),
                "rule": risk.get("rule", ""),
                "severity": risk.get("severity", "medium"),
                "timeout": timeout,
            }
            await self._broadcast(_make_event(EVT_GAME_ALERT, payload))

            try:
                approved = await asyncio.wait_for(future, timeout=timeout)
                return bool(approved)
            except asyncio.TimeoutError:
                # default-safe：超時 → 放行 TTS
                return True
            finally:
                self._pending_alerts.pop(alert_id, None)
        except Exception as e:
            logger.warning(f"[Companion_Bridge] request_radar_veto 失敗 (放行 TTS): {e}")
            return True

    async def _handle_game_end(self, payload: dict[str, Any]) -> None:
        """收 game_end → 呼叫 cog.end_session。"""
        game_name = payload.get("game", "")
        cog = self._resolve_game_cog(game_name)
        if cog is None:
            return
        method = getattr(cog, "end_session", None)
        if method is None:
            logger.info(f"[Companion_Bridge] cog 缺 end_session 方法，drop game={game_name}")
            return
        try:
            await method()
        except Exception as e:
            logger.warning(f"[Companion_Bridge] cog.end_session 失敗: {e}")

    # ── Emitter API（Marvin pipeline 呼叫）─────────────────────────────────

    async def _broadcast(self, event: dict[str, Any]) -> None:
        """廣播給所有 client，遇到斷線的就清掉。"""
        if not self._clients:
            return
        text = json.dumps(event, ensure_ascii=False, default=str)
        dead: list[web.WebSocketResponse] = []
        # snapshot 避免迭代時被改
        for ws in list(self._clients):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_str(text)
            except Exception as e:
                logger.debug(f"[Companion_Bridge] send 失敗，標記移除: {e}")
                dead.append(ws)
        if dead:
            async with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def emit_stt_chunk(self, speaker: str, text: str, engine: str) -> None:
        await self._broadcast(_make_event(EVT_STT_CHUNK, {
            "speaker": speaker, "text": text, "engine": engine,
        }))

    async def emit_intent_routed(self, intent: str, query: str, target_user: str | None = None) -> None:
        await self._broadcast(_make_event(EVT_INTENT_ROUTED, {
            "intent": intent, "query": query, "target_user": target_user,
        }))

    async def emit_tts_started(self, text: str, voice: str, target: str | None = None) -> None:
        await self._broadcast(_make_event(EVT_TTS_STARTED, {
            "text": text, "voice": voice, "target": target,
        }))

    async def emit_tts_done(self) -> None:
        await self._broadcast(_make_event(EVT_TTS_DONE, {}))

    async def emit_music_started(self, song_info: dict[str, Any], requested_by: str) -> None:
        """音樂播放開始時呼叫；payload 含 title / style / target / started_ts / source。"""
        payload = {
            "title": song_info.get("title", ""),
            "style": song_info.get("style", ""),
            "target": song_info.get("target") or requested_by,
            "started_ts": song_info.get("started_ts") or time.time(),
            "source": song_info.get("source", ""),
            "requested_by": requested_by,
        }
        await self._broadcast(_make_event(EVT_MUSIC_STARTED, payload))

    async def emit_music_ended(self, song_info: dict[str, Any], completion: str) -> None:
        """音樂播放結束時呼叫；completion: 'natural' | 'skipped' | 'stopped'。"""
        payload = {
            "title": song_info.get("title", ""),
            "completion": completion,
        }
        await self._broadcast(_make_event(EVT_MUSIC_ENDED, payload))

    async def emit_music_reaction(self, username: str, song_info: dict[str, Any], reaction: str) -> None:
        """玩家對音樂的反應；reaction: 'love' | 'skip' | 'hum' | 'silent'。"""
        payload = {
            "username": username,
            "title": song_info.get("title", ""),
            "reaction": reaction,
        }
        await self._broadcast(_make_event(EVT_MUSIC_REACTION, payload))

    async def emit_game_phase_changed(
        self, game_name: str, phase: str, payload: dict[str, Any]
    ) -> None:
        """遊戲狀態轉換時呼叫；廣播 game_phase_changed 給所有 client。

        payload 內含當下階段有用的資料（current_player / round / scoreboard /
        timer_seconds / last_event …），game / phase 由方法注入。
        """
        full_payload: dict[str, Any] = {"game": game_name, "phase": phase}
        if isinstance(payload, dict):
            for k, v in payload.items():
                # game / phase 不可被 payload 覆寫
                if k in ("game", "phase"):
                    continue
                full_payload[k] = v
        await self._broadcast(_make_event(EVT_GAME_PHASE_CHANGED, full_payload))

    async def emit_member_joined(
        self, speaker: str, payload_extras: dict[str, Any] | None = None
    ) -> None:
        """玩家加入 Marvin 所在語音頻道時呼叫。

        payload：{speaker, joined_ts, ...extras}。
        extras 可帶 name / marvin / stage / relationship 等顯示用欄位；
        speaker 與 joined_ts 為必有欄位，extras 不得覆寫它們。
        """
        payload: dict[str, Any] = {"speaker": speaker, "joined_ts": time.time()}
        if isinstance(payload_extras, dict):
            for k, v in payload_extras.items():
                if k in ("speaker", "joined_ts"):
                    continue
                payload[k] = v
        await self._broadcast(_make_event(EVT_MEMBER_JOINED, payload))

    async def emit_member_left(self, speaker: str) -> None:
        """玩家離開 Marvin 所在語音頻道時呼叫。payload：{speaker, left_ts}。"""
        payload = {"speaker": speaker, "left_ts": time.time()}
        await self._broadcast(_make_event(EVT_MEMBER_LEFT, payload))

    async def emit_voice_channel_snapshot(self, members: list[dict[str, Any]]) -> None:
        """當前語音頻道成員的快照（新 client 連上時用）。

        payload：{members: [{speaker, ...}], snapshot_ts}。members 是 caller
        準備好的 dict 清單，bridge 不修改其欄位（讓上游決定 marvin / stage 等）。
        """
        payload = {"members": list(members or []), "snapshot_ts": time.time()}
        await self._broadcast(_make_event(EVT_VOICE_CHANNEL_SNAPSHOT, payload))

    async def emit_atmosphere_snapshot(self) -> None:
        """讀 tracker 當前 snapshot，廣播。"""
        snap = self._tracker.get_snapshot()
        payload = {
            "dominant_topic": getattr(snap, "dominant_topic", "casual"),
            "topic_confidence": getattr(snap, "topic_confidence", 0.0),
            "room_mood": getattr(snap, "room_mood", ""),
            "speaker_states": getattr(snap, "speaker_states", {}) or {},
            "recent_topics": getattr(snap, "recent_topics", []) or [],
            "snapshot_ts": getattr(snap, "ts", time.time()),
        }
        await self._broadcast(_make_event(EVT_ATMOSPHERE_SNAPSHOT, payload))
