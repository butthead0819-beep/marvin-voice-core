"""STTHistory → Discord 文字頻道鏡像（10/21 使用者：穩定前要在文字頻道看到 STT / 回答 / 用了哪個 LLM）。

只鏡像三種行：[⚡喚醒] 使用者原話、[✅Query通過] 清理後（跟原話不同才貼）、[BOT→X] 回答（含模型來源標籤）。
批次：累積 FLUSH_S 秒合併成一則訊息，避開 Discord 頻率限制。MARVIN_TRANSCRIPT_MIRROR=0 關閉（main_discord 判斷）。
"""
from __future__ import annotations

import asyncio
import logging
import re

FLUSH_S = 1.5
MAX_MSG_CHARS = 1900

_WAKE_RE = re.compile(r"^\[⚡喚醒\] \[(?P<spk>[^\]]+)\] raw='(?P<text>.*)' \| Track=")
_QUERY_RE = re.compile(r"^\[✅Query通過\] \[(?P<spk>[^\]]+)\] \S+ \| query='(?P<text>.*)'$")
_BOT_RE = re.compile(r"^\[BOT→(?P<spk>[^\]]+)\] \((?P<tag>[^)]*)\) (?P<text>.*)$", re.S)

logger = logging.getLogger(__name__)


def format_mirror_line(msg: str, last_raw: dict[str, str]) -> str | None:
    """STTHistory 一行 → 頻道要貼的一行，或 None（不鏡像）。last_raw：speaker → 最近原話（會被更新）。"""
    m = _WAKE_RE.match(msg)
    if m:
        last_raw[m["spk"]] = m["text"]
        return f"🎙️ **{m['spk']}**：{m['text']}"
    m = _QUERY_RE.match(msg)
    if m:
        if m["text"] in last_raw.get(m["spk"], ""):
            return None   # 清理後跟原話一樣（只是剝掉喚醒詞）→ 不重貼
        return f"🧹 **{m['spk']}**（清理後）：{m['text']}"
    m = _BOT_RE.match(msg)
    if m:
        return f"🤖 → **{m['spk']}**〔{m['tag']}〕{m['text']}"
    return None


class TranscriptMirror(logging.Handler):
    def __init__(self, loop: asyncio.AbstractEventLoop, get_channel, flush_s: float = FLUSH_S):
        super().__init__(level=logging.INFO)
        self._loop = loop
        self._get_channel = get_channel
        self._flush_s = flush_s
        self._buf: list[str] = []
        self._task: asyncio.Task | None = None
        self._last_raw: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = format_mirror_line(record.getMessage(), self._last_raw)
            if line:
                self._loop.call_soon_threadsafe(self._enqueue, line)   # emit 可能在 to_thread 執行緒
        except Exception:
            pass   # 鏡像絕不能影響主流程

    def _enqueue(self, line: str) -> None:
        self._buf.append(line)
        if self._task is None or self._task.done():
            self._task = self._loop.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(self._flush_s)
        lines, self._buf = self._buf, []
        channel = self._get_channel()
        if channel is None or not lines:
            return
        import discord
        chunk = ""
        for line in lines:
            line = line[:MAX_MSG_CHARS]
            if chunk and len(chunk) + 1 + len(line) > MAX_MSG_CHARS:
                await self._send(channel, chunk, discord)
                chunk = ""
            chunk = f"{chunk}\n{line}" if chunk else line
        if chunk:
            await self._send(channel, chunk, discord)

    async def _send(self, channel, text: str, discord) -> None:
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            logger.debug(f"[TranscriptMirror] 送出失敗（忽略）：{e}")


def install_transcript_mirror(loop, get_channel) -> TranscriptMirror:
    """掛到 STTHistory logger（冪等：已掛過就回傳既有那個）。"""
    stt_logger = logging.getLogger("STTHistory")
    for h in stt_logger.handlers:
        if isinstance(h, TranscriptMirror):
            return h
    h = TranscriptMirror(loop, get_channel)
    stt_logger.addHandler(h)
    return h
