"""CloudflareTunnel — 自動啟動 Quick Tunnel，抓到 URL 後回傳。

使用方式（在 main_discord.py 的 on_ready 裡）：
    tunnel = CloudflareTunnel(port=8767)
    url = await tunnel.start()   # 等到 URL 出現（約 5 秒）
    os.environ["GAME_PUBLIC_URL"] = url

關閉時：
    await tunnel.stop()
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

logger = logging.getLogger("CloudflareTunnel")

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class CloudflareTunnel:
    def __init__(self, port: int = 8767, timeout: float = 30.0):
        self._port = port
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._url: str | None = None

    @property
    def url(self) -> str | None:
        return self._url

    async def start(self) -> str | None:
        """啟動 cloudflared，等待並回傳 tunnel URL。逾時回傳 None。"""
        cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{self._port}"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("[CloudflareTunnel] cloudflared 未安裝（brew install cloudflared）")
            return None

        self._url = await asyncio.wait_for(
            self._read_until_url(), timeout=self._timeout
        )
        if self._url:
            logger.info(f"[CloudflareTunnel] tunnel URL: {self._url}")
            # 同步寫到固定路徑，方便外部查詢（bot restart 後 URL 會變）
            try:
                import os
                log_dir = os.path.expanduser("~/Library/Logs/Marvin")
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, "tunnel_url.txt"), "w") as f:
                    f.write(self._url + "\n")
            except Exception as e:
                logger.warning(f"[CloudflareTunnel] 無法寫入 tunnel_url.txt: {e}")
        else:
            logger.warning("[CloudflareTunnel] 無法取得 tunnel URL")
        return self._url

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    async def _read_until_url(self) -> str | None:
        """從 stderr 逐行讀，找到 trycloudflare.com URL 就回傳。"""
        if self._proc is None or self._proc.stderr is None:
            return None
        async for line_bytes in self._proc.stderr:
            line = line_bytes.decode(errors="replace")
            m = _URL_RE.search(line)
            if m:
                return m.group(0)
        return None
