"""ErrorDispatcher — route Marvin's real errors to a forensic incident report + DM.

設計重點：
  - 黑名單擋雜訊（rtcp/yt-dlp/syncedlyrics/discord.player/自願 restart/Groq TPM）
  - 白名單觸發（unhandled traceback、Tier-1 Exhausted、App Command Error、CRITICAL）
  - 5-min signature dedup（normalized message 前 80 字 + logger + level）
  - self-loop guard：dispatcher 自己 emit 的 ERROR 一律忽略
  - emit() 在 logging thread 跑，必須非阻塞；任務丟到 event loop
  - 鑑識交給 incident_writer（純 Python），LLM 不參與；Claude Code 後續處理
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


# ── 過濾規則 ──────────────────────────────────────────────────────────────────

# Logger 名直接黑名單（這些來源的 ERROR 99% 是噪音）
_NOISE_LOGGER_PREFIXES = (
    "discord.ext.voice_recv",
    "discord.player",
    "syncedlyrics",
)

# Logger 名 + 訊息子字串雙重黑名單
_NOISE_MESSAGE_PATTERNS = (
    re.compile(r"yt-dlp.*Resource deadlock"),
    re.compile(r"yt-dlp.*not available"),
    re.compile(r"\[Restart\].*手動重啟"),
    re.compile(r"\[Restart\].*os\.execv"),
    re.compile(r"\[TPM Guard\]"),
    re.compile(r"幻覺偵測"),
)

# 白名單規則：(logger_name_prefix or None, message_substring)
_WHITELIST_RULES = (
    ("gemini_router_llm", "Tier-1 Exhausted"),
    ("MarvinBot", "App Command Error"),
    ("MarvinBot", "Sentinel"),
)

# Signature 標準化（避免 address / 時戳 / 流水號干擾 dedup）
_NORMALIZE_PATTERNS = (
    (re.compile(r"0x[0-9a-f]+"), "<addr>"),
    (re.compile(r"\b\d{4,}\b"), "<N>"),
)


IncidentWriter = Callable[[logging.LogRecord, int], Path]
"""Signature: (record, recurrence_24h) -> path to written markdown."""


class ErrorDispatcher(logging.Handler):
    """Logging handler that forensically logs filtered errors as markdown + DM owner.

    Forensic extraction is delegated to ``incident_writer`` (pure Python, no LLM).
    The DM is a thin pointer to the report file so Claude Code can pick it up.
    """

    def __init__(
        self,
        *,
        incident_writer: IncidentWriter,
        dm_sender: Callable[[str], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
        cooldown_seconds: int = 300,
        writer_timeout_seconds: float = 10.0,
    ):
        super().__init__(level=logging.ERROR)
        self.incident_writer = incident_writer
        self.dm_sender = dm_sender
        self.loop = loop
        self.cooldown = cooldown_seconds
        # 確保 writer 卡死不會永久 block 後續派發（_inflight 自鎖）
        self.writer_timeout_seconds = writer_timeout_seconds
        self._last_seen: dict[tuple, float] = {}
        # signature → 24h rolling count (記錄出現次數以填 recurrence_24h)
        self._signature_counts: dict[tuple, list[float]] = {}
        self._lock = threading.Lock()
        # self-loop guard：dispatcher 任務跑中時忽略所有 ERROR（避免無窮迴圈）
        self._inflight = False

    # ── public logging.Handler API ──

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit(record)
        except Exception:  # noqa: BLE001 — logging path must never raise
            pass

    # ── filter pipeline ──

    def _emit(self, record: logging.LogRecord) -> None:
        if self._inflight:
            return
        if record.levelno < logging.ERROR:
            return
        # self-loop guard：dispatcher 自己 log 的 ERROR 直接擋
        if record.name == __name__ or record.name.startswith(__name__ + "."):
            return

        # Noise filter
        for prefix in _NOISE_LOGGER_PREFIXES:
            if record.name.startswith(prefix):
                return
        msg = record.getMessage()
        for pat in _NOISE_MESSAGE_PATTERNS:
            if pat.search(msg):
                return

        # Whitelist
        if not self._matches_whitelist(record, msg):
            return

        # Dedup + recurrence tracking
        sig = self._signature(record, msg)
        now = time.time()
        with self._lock:
            # 紀錄這次發生時間到 rolling window；count = 過去 24h 內出現次數
            window = self._signature_counts.setdefault(sig, [])
            cutoff = now - 86400
            window[:] = [t for t in window if t > cutoff]
            window.append(now)
            recurrence_24h = len(window)

            last = self._last_seen.get(sig, 0.0)
            if now - last < self.cooldown:
                return
            self._last_seen[sig] = now

        # Dispatch (non-blocking)
        try:
            asyncio.run_coroutine_threadsafe(
                self._dispatch(record, recurrence_24h), self.loop
            )
        except RuntimeError:
            # loop 已關閉 / 尚未啟動 — 忽略
            pass

    def _matches_whitelist(self, record: logging.LogRecord, msg: str) -> bool:
        if record.exc_info:
            return True
        if record.levelno >= logging.CRITICAL:
            return True
        for logger_prefix, msg_sub in _WHITELIST_RULES:
            if logger_prefix and not record.name.startswith(logger_prefix):
                continue
            if msg_sub in msg:
                return True
        return False

    def _signature(self, record: logging.LogRecord, msg: str) -> tuple:
        norm = msg
        for pat, repl in _NORMALIZE_PATTERNS:
            norm = pat.sub(repl, norm)
        return (record.name, record.levelno, norm[:80])

    # ── async dispatch ──

    async def _dispatch(self, record: logging.LogRecord, recurrence_24h: int) -> None:
        self._inflight = True
        try:
            # incident_writer 是同步 I/O；丟到 thread 避免阻塞 loop。
            # wait_for 包住，避免 disk full / 慢 I/O 把 _inflight 卡到永久 block 後續派發。
            try:
                path = await asyncio.wait_for(
                    asyncio.to_thread(self.incident_writer, record, recurrence_24h),
                    timeout=self.writer_timeout_seconds,
                )
                summary = (
                    f"🚨 **Incident recorded**\n"
                    f"`{record.name}` / `{record.levelname}` "
                    f"(recurrence: {recurrence_24h}× / 24h)\n"
                    f"📄 `{path}`\n"
                    f"→ open Claude Code to triage."
                )
            except asyncio.TimeoutError:
                summary = (
                    f"🚨 **Error fired but writer timeout**\n"
                    f"`{record.name}` / `{record.levelname}` "
                    f"(timeout after {self.writer_timeout_seconds}s — disk slow / full?)\n"
                    f"```\n{record.getMessage()[:400]}\n```"
                )
            except Exception as exc:  # noqa: BLE001
                summary = (
                    f"🚨 **Error fired but report failed**\n"
                    f"`{record.name}` / `{record.levelname}`\n"
                    f"```\n{record.getMessage()[:400]}\n```\n"
                    f"(incident_writer error: {exc})"
                )
            try:
                await self.dm_sender(summary)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._inflight = False
