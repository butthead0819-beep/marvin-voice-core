"""StreamingSTTSession — 串流 STT daemon 管理 + 語意斷句整合（Volatile Phase 1）。

把 stream_stt_daemon_bin 的 volatile JSONL 餵進 SemanticEndpointer；斷句決策
（或 daemon final 兜底）觸發 on_cut(text, meta)，讓 caller 提前發動 pipeline，
省掉 VAD 純靜默等待。

分層：
- on_daemon_line / begin_utterance：純狀態機（可單測，不碰 subprocess）
- start / feed / finalize / stop：subprocess IO shell（daemon 已端到端煙霧）

設計約束：
- daemon 常駐暖模型（一個 engine 實例一個 daemon）；crash → 標記不可用、caller 降級
- 每語句只切一次（語意斷句優先，daemon final 兜底）
- 任何 daemon 失敗只降級回既有 VAD+batch，絕不阻斷主管線
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Callable, Optional

from streaming_endpointer import SemanticEndpointer

logger = logging.getLogger(__name__)

_DAEMON_BIN = "./stream_stt_daemon_bin"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def streaming_enabled() -> bool:
    return os.environ.get("STT_STREAMING", "").strip().lower() in _TRUE_VALUES


class StreamingSTTSession:
    def __init__(self, on_cut: Callable[[str, dict], None], *,
                 stability_window_ms: int = 800, min_duration_ms: int = 300,
                 daemon_bin: str = _DAEMON_BIN):
        self._on_cut = on_cut
        self._stability_window_ms = stability_window_ms
        self._min_duration_ms = min_duration_ms
        self._daemon_bin = daemon_bin
        self._ep = SemanticEndpointer(
            stability_window_ms=stability_window_ms, min_duration_ms=min_duration_ms)
        self._cut_done = False
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self.available = True  # daemon 健康旗標；crash → False，caller 降級

    # ── 純狀態機（可單測）────────────────────────────────────────────────────

    def begin_utterance(self, temperature: str | None = None) -> None:
        """開新語句：重置 endpointer（溫度給定則重設穩定窗）+ 清切旗標。"""
        if temperature is not None:
            self._ep = SemanticEndpointer.from_temperature(
                temperature, min_duration_ms=self._min_duration_ms)
        else:
            self._ep.reset()
        self._cut_done = False

    def on_daemon_line(self, line: str) -> None:
        """處理一行 daemon JSONL。volatile→endpointer；final→兜底切。"""
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            return
        if obj.get("ready"):
            self._ready.set()
            return
        if self._cut_done:
            return
        if "final" in obj:
            self._fire(obj["final"], source="daemon_final",
                       revision_count=self._ep._revisions)
            return
        if "v" in obj:
            d = self._ep.observe(int(obj.get("t_ms", 0)), obj["v"])
            if d is not None:
                self._fire(d.text, source="semantic_endpoint",
                           revision_count=d.revision_count)

    def _fire(self, text: str, *, source: str, revision_count: int) -> None:
        if self._cut_done or not text.strip():
            return
        self._cut_done = True
        try:
            self._on_cut(text, {"source": source, "revision_count": revision_count})
        except Exception as e:
            logger.warning(f"[StreamSTT] on_cut raised: {e}")

    # ── subprocess IO shell ──────────────────────────────────────────────────

    async def start(self, *, ready_timeout: float = 10.0) -> bool:
        """spawn daemon、等暖模型 ready。失敗回 False（caller 降級）。"""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._daemon_bin,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "STT_LOCALE": os.environ.get("STT_LOCALE", "zh-TW")},
            )
        except Exception as e:
            logger.warning(f"[StreamSTT] daemon 啟動失敗: {e}")
            self.available = False
            return False
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=ready_timeout)
        except asyncio.TimeoutError:
            logger.warning("[StreamSTT] daemon 暖機逾時")
            self.available = False
            return False
        return True

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            async for raw in self._proc.stdout:
                self.on_daemon_line(raw.decode("utf-8", errors="ignore"))
        except Exception as e:
            logger.warning(f"[StreamSTT] read loop 結束: {e}")
        finally:
            self.available = False  # daemon 死了 → 降級

    def _send(self, line: str) -> None:
        if self._proc and self._proc.stdin and not self._proc.stdin.is_closing():
            try:
                self._proc.stdin.write((line + "\n").encode())
            except Exception:
                self.available = False

    def begin(self, temperature: str | None = None) -> None:
        """live 開語句：reset 狀態 + 送 daemon R。"""
        self.begin_utterance(temperature)
        self._send("R")

    def feed(self, pcm16_16k: bytes) -> None:
        """餵一塊 16kHz mono int16 PCM（Sink 已降頻格式）。"""
        if pcm16_16k:
            self._send("A " + base64.b64encode(pcm16_16k).decode())

    def finalize(self) -> None:
        self._send("F")

    async def stop(self) -> None:
        if self._proc:
            try:
                if self._proc.stdin and not self._proc.stdin.is_closing():
                    self._proc.stdin.close()
                self._proc.terminate()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
