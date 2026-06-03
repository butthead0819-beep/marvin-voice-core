import hmac
import os
import time
import asyncio
import logging
from aiohttp import web

logger = logging.getLogger("MarvinBot.MarmoServer")

MARMO_PORT = int(os.getenv("MARMO_PORT", "8765"))
MARMO_VOICE = os.getenv("MARMO_VOICE", "en-US-GuyNeural")
MARMO_TOKEN = os.getenv("MARMO_TOKEN", "")


def _dual_speak_enabled() -> bool:
    """env MARMO_DUAL_SPEAK gate（每次讀，hot-flippable）。

    開啟條件：MARMO_DUAL_SPEAK in {"1", "true", "yes"}（大小寫不敏感）。
    其他值或未設 → False（保持既有 play_tts 直接路徑）。
    """
    return os.getenv("MARMO_DUAL_SPEAK", "").strip().lower() in ("1", "true", "yes")


class MarmoServer:
    """
    Async HTTP webhook server that receives Marmo job results and voices them
    through the active Discord voice channel via VoiceController.play_tts().

    Two Marmo paths exist in this bot:
      1. _handle_marmo_query() — Discord wait_for: Marvin asks @AI Marmo via text
         channel and waits up to 90s for a reply. Synchronous request-response.
      2. This server (MarmoServer) — HTTP webhook: Marmo POSTs completed job results
         here asynchronously, including proactive alerts with no prior voice command.
    Contract: Marmo must not use both paths for the same job. The HTTP path is
    preferred for all new NemoClaw integrations.
    """

    def __init__(self, voice_controller):
        self._vc = voice_controller
        self._seen_jobs: set[str] = set()
        self._runner = None

    async def _handle_result(self, request: web.Request) -> web.Response:
        if MARMO_TOKEN and not hmac.compare_digest(request.headers.get("X-Marmo-Token", ""), MARMO_TOKEN):
            return web.Response(status=401, text="unauthorized")

        data = await request.json()
        text = data.get("text", "").strip()
        job_id = data.get("job_id", "")

        if not text:
            return web.Response(status=400, text="empty text")

        if getattr(self._vc, "game_mode", False):
            return web.Response(text="game_mode_active")  # 遊戲中靜默丟棄 Marmo webhook

        if job_id and job_id in self._seen_jobs:
            return web.Response(text="duplicate")
        if job_id:
            self._seen_jobs.add(job_id)
            if len(self._seen_jobs) > 200:
                self._seen_jobs.pop()  # evicts arbitrary element (best-effort dedup)

        # 🎭 [Marmo 一搭一唱 T9] env MARMO_DUAL_SPEAK=true 走 IntentBus dispatch；
        # bus 不可用（vc._intent_bus 是 None / 沒這 attr）→ fallback 走既有 play_tts。
        # Flag off → 完全不走這條，等同改動前行為。
        if _dual_speak_enabled():
            bus = getattr(self._vc, "_intent_bus", None)
            if bus is not None:
                from intent_bus import IntentContext
                stream_active = bool(getattr(self._vc, "stream_mode", False))
                ctx = IntentContext(
                    speaker="marmo_server",
                    raw_text=text, query=text, original_raw=None,
                    wake_intent=None,
                    stream_active=stream_active,
                    game_mode=False,  # 上方 gate 已守
                    is_owner=False,
                    now=time.time(),
                    mode=("stream" if stream_active else "normal"),
                    dispatch_source="marmo_inject",
                    # pattern：optional override（測試後門）。預設 None → agent 走 marmo_lead；
                    # 帶 "marvin_lead" 可用 webhook 聽 Case B。
                    payload={"text": text, "job_id": job_id, "pattern": data.get("pattern"),
                             "interject": data.get("interject"),  # 手動測打岔：curl 帶 interject=true
                             "segments": data.get("segments"),    # 測播放：帶現成 segments 跳過 LLM 生成
                             "duck": data.get("duck"), "step": data.get("step")},  # 即時調 fade 終點/速度
                )
                dispatch_task = asyncio.create_task(bus.dispatch(ctx))

                def _log_dispatch_exc(t: asyncio.Task):
                    exc = t.exception()
                    if exc:
                        logger.error(f"[MarmoServer] bus.dispatch raised: {exc}", exc_info=exc)

                dispatch_task.add_done_callback(_log_dispatch_exc)
                return web.Response(text="ok")
            logger.warning("[MarmoServer] MARMO_DUAL_SPEAK on but vc._intent_bus 不可用、fallback play_tts")

        task = asyncio.create_task(
            self._vc.play_tts(text, already_in_channel=True, voice=MARMO_VOICE, emotion_tag="neutral")
        )

        def _log_exc(t: asyncio.Task):
            exc = t.exception()
            if exc:
                logger.error(f"[MarmoServer] play_tts raised: {exc}", exc_info=exc)

        task.add_done_callback(_log_exc)
        return web.Response(text="ok")

    async def start(self):
        app = web.Application()
        app.router.add_post("/marmo-result", self._handle_result)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", MARMO_PORT)
        try:
            await site.start()
            logger.info(f"[MarmoServer] Listening on 127.0.0.1:{MARMO_PORT}")
        except OSError as e:
            logger.warning(f"[MarmoServer] Could not bind port {MARMO_PORT}: {e} — Marmo webhook unavailable")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
