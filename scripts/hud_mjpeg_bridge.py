"""HUD MJPEG bridge — headless Chrome 在 Mac 端渲染 /hud，定期 Page.captureScreenshot
轉成 MJPEG 串流，讓 Pi 端只需要 mpv --vo=drm 播放（不必再跑 chromium+cage 算圖）。

見 [[project_hud_pi3b_kiosk_bringup]]：Pi 3B 軟體跑 chromium GPU 算圖會把 SoC 從
54°C 拉到 64-70°C，這隻 bridge 把運算丟回 Mac，Pi 只做 JPEG 解碼＋顯示。

⚠️ 原本想用 CDP Page.startScreencast（只在重繪時推 frame），但 headless Chrome
背景分頁的 rAF 動畫會被節流到幾乎不動，實測整個連線只收到一張 frame 就不動了。
改成固定週期主動 captureScreenshot 輪詢，不依賴瀏覽器自己判斷有沒有重繪。

用法：python3 scripts/hud_mjpeg_bridge.py
Pi 端：mpv --vo=drm http://<mac-tailscale-ip>:8791/stream.mjpg
"""
import asyncio
import base64
import json
import os
import subprocess
import sys

from aiohttp import web, ClientSession

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9333
BRIDGE_PORT = int(os.getenv("MARVIN_HUD_STREAM_PORT", "8791"))
HUD_WIDTH, HUD_HEIGHT = 1920, 480  # HUD v12 原生設計比例，見 main_satellite.py HUD_HTML
CAPTURE_INTERVAL = float(os.getenv("MARVIN_HUD_STREAM_INTERVAL", "0.1"))  # ~10fps


def _hud_url() -> str:
    token = os.getenv("MARVIN_TEXT_TOKEN", "").strip()
    base = f"http://localhost:{os.getenv('MARVIN_TEXT_PORT', '8790')}/hud"
    return f"{base}?t={token}" if token else base


class ScreencastSource:
    """管一份 headless Chrome + CDP screencast，frame 進來廣播給所有訂閱者。"""

    def __init__(self):
        self._proc = None
        self._subscribers = set()
        self._task = None

    async def start(self):
        self._proc = subprocess.Popen([
            CHROME_BIN, "--headless=new", f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir=/tmp/marvin-hud-chrome-profile",
            f"--window-size={HUD_WIDTH},{HUD_HEIGHT}", "--disable-gpu",
            "--hide-scrollbars", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(self._on_done)

    @staticmethod
    def _on_done(task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            import traceback
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    async def _run(self):
        async with ClientSession() as http:
            for _ in range(50):
                try:
                    resp = await http.put(f"http://localhost:{CDP_PORT}/json/new?{_hud_url()}")
                    tab = await resp.json()
                    break
                except Exception:
                    await asyncio.sleep(0.2)
            else:
                print("Chrome CDP 起不來", file=sys.stderr)
                return
            ws_url = tab["webSocketDebuggerUrl"]
            async with http.ws_connect(ws_url, max_msg_size=0) as ws:
                msg_id = 0
                pending = {}

                async def call(method, params=None):
                    nonlocal msg_id
                    msg_id += 1
                    this_id = msg_id
                    fut = asyncio.get_event_loop().create_future()
                    pending[this_id] = fut
                    await ws.send_json({"id": this_id, "method": method, "params": params or {}})
                    return await fut

                async def _pump():
                    async for raw in ws:
                        data = json.loads(raw.data)
                        if "id" in data and data["id"] in pending:
                            pending.pop(data["id"]).set_result(data.get("result"))

                pump_task = asyncio.create_task(_pump())
                await call("Page.enable")
                await call("Emulation.setDeviceMetricsOverride", {
                    "width": HUD_WIDTH, "height": HUD_HEIGHT, "deviceScaleFactor": 1, "mobile": False})

                try:
                    while True:
                        result = await call("Page.captureScreenshot", {
                            "format": "jpeg", "quality": 85,
                            "clip": {"x": 0, "y": 0, "width": HUD_WIDTH, "height": HUD_HEIGHT, "scale": 1}})
                        jpeg = base64.b64decode(result["data"])
                        self._broadcast(jpeg)
                        await asyncio.sleep(CAPTURE_INTERVAL)
                finally:
                    pump_task.cancel()

    def _broadcast(self, jpeg: bytes):
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(jpeg)

    def subscribe(self) -> "asyncio.Queue[bytes]":
        q: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=2)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)

    def stop(self):
        if self._task:
            self._task.cancel()
        if self._proc:
            self._proc.terminate()


source = ScreencastSource()


async def handle_stream(request):
    boundary = "hudframe"
    resp = web.StreamResponse(headers={
        "Content-Type": f"multipart/x-mixed-replace; boundary={boundary}",
        "Cache-Control": "no-cache",
    })
    await resp.prepare(request)
    q = source.subscribe()
    try:
        while True:
            jpeg = await q.get()
            await resp.write(
                f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n".encode()
                + jpeg + b"\r\n")
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        source.unsubscribe(q)
    return resp


async def on_startup(app):
    await source.start()


async def on_cleanup(app):
    source.stop()


def main():
    app = web.Application()
    app.router.add_get("/stream.mjpg", handle_stream)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=BRIDGE_PORT)


if __name__ == "__main__":
    main()
