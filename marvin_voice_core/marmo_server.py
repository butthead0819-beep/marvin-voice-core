import os
import asyncio
import logging
from aiohttp import web

logger = logging.getLogger("MarvinBot.MarmoServer")

MARMO_PORT = int(os.getenv("MARMO_PORT", "8765"))
MARMO_VOICE = os.getenv("MARMO_VOICE", "en-US-GuyNeural")
MARMO_TOKEN = os.getenv("MARMO_TOKEN", "")


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
        if MARMO_TOKEN and request.headers.get("X-Marmo-Token") != MARMO_TOKEN:
            return web.Response(status=401, text="unauthorized")

        data = await request.json()
        text = data.get("text", "").strip()
        job_id = data.get("job_id", "")

        if not text:
            return web.Response(status=400, text="empty text")

        if job_id and job_id in self._seen_jobs:
            return web.Response(text="duplicate")
        if job_id:
            self._seen_jobs.add(job_id)
            if len(self._seen_jobs) > 200:
                self._seen_jobs.pop()  # evicts arbitrary element (best-effort dedup)

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
