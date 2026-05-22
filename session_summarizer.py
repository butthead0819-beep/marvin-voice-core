"""
session_summarizer.py

每 5 分鐘對語音逐字稿窗口呼叫一次 LLM，產出：
  - summary_text：這個窗口發生了什麼（自然語言）
  - commitments：多人討論時的承諾/待辦，透過 on_commitment_detected callback 送出確認

設計原則：
  - 單人窗口（自言自語）→ 只存摘要，不送承諾（需 is_manual_add_query 顯式觸發）
  - 多人討論窗口 → 承諾透過 callback 進確認佇列，等靜默時 Marvin 詢問確認
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from typing import Callable

from recall_handler import PendingConfirmation

logger = logging.getLogger(__name__)

_MIN_UTTERANCES = 3
_GROQ_MODEL = "llama-3.1-8b-instant"
_TIMEOUT = 15.0
_CONFIRMATION_TTL = 1800  # 30 分鐘後過期丟棄

_SYSTEM_PROMPT = """\
你是一個語音對話摘要助理。以下是一段語音對話的逐字稿。
請輸出合法的 JSON，格式如下：
{
  "summary": "一句話描述這段對話的重點",
  "commitments": [
    {
      "speaker": "說話者名稱",
      "text": "承諾或待辦的內容（簡短描述）",
      "type": "promise 或 todo",
      "target": "交辦對象（沒有則 null）",
      "due_date": null
    }
  ]
}
只輸出 JSON，不要有其他文字。commitments 若無則回傳空陣列。
"""


def commitment_to_callback(conf) -> tuple[str, str] | None:
    """commitment（PendingConfirmation）→ 主動 callback (speaker, text)，或 None=跳過。

    只處理 inbound（speaker 自己的承諾）→ 之後返場時提醒本人「你上次說要X」。
    這是自我提醒（把你自己的公開承諾講回給你），低隱私風險 → enqueue 時 shareable=True。
    outbound（叫別人做的）= 跨人 relay，不在此範圍（deferred）。
    """
    if conf is None or getattr(conf, "direction", None) != "inbound":
        return None
    text = (getattr(conf, "task_text", "") or "").strip()
    speaker = (getattr(conf, "speaker", "") or "").strip()
    if not text or not speaker:
        return None
    return (speaker, text)


class SessionSummarizer:
    def __init__(
        self,
        transcript_store,
        summary_store,
        groq_client,
        owner_speaker: str,
        on_commitment_detected: Callable[[PendingConfirmation], None] | None = None,
    ):
        self.transcript_store = transcript_store
        self.summary_store = summary_store
        self.groq_client = groq_client
        self.owner_speaker = owner_speaker
        self.on_commitment_detected = on_commitment_detected
        self._task: asyncio.Task | None = None

    async def start(self, guild_id: int, interval_seconds: int = 300) -> None:
        self._task = asyncio.create_task(self._loop(guild_id, interval_seconds))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self, guild_id: int, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            await self.summarize_window(guild_id, now - interval, now)

    async def summarize_window(
        self, guild_id: int, window_start: float, window_end: float
    ) -> None:
        utterances = self.transcript_store.get_recent(
            speaker=None,
            guild_id=guild_id,
            minutes=int((window_end - window_start) / 60) + 1,
        )
        utterances = [u for u in utterances if window_start <= u["timestamp"] <= window_end]

        if len(utterances) < _MIN_UTTERANCES:
            logger.debug(f"[Summarizer] 窗口 utterances={len(utterances)} < {_MIN_UTTERANCES}，跳過")
            return

        speakers = list({u["speaker"] for u in utterances})
        is_multi_speaker = len(speakers) > 1
        transcript_text = self._format_transcript(utterances)
        raw_text = "\n".join(u["text"] for u in utterances)

        try:
            response = await asyncio.wait_for(
                self.groq_client.chat.completions.create(
                    model=_GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": transcript_text},
                    ],
                    temperature=0.3,
                    max_tokens=500,
                    stream=False,
                ),
                timeout=_TIMEOUT,
            )
            content = response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"[Summarizer] LLM 呼叫失敗: {e}")
            return

        summary_text, commitments = self._parse_llm_output(content)

        self.summary_store.save_summary(
            guild_id=guild_id,
            window_start=window_start,
            window_end=window_end,
            summary_text=summary_text,
            speakers=speakers,
        )

        # 多人討論才送 callback；單人窗口靠 is_manual_add_query 顯式觸發
        if is_multi_speaker and self.on_commitment_detected:
            for c in commitments:
                speaker = c.get("speaker", "")
                target = c.get("target") or None
                commitment_type = c.get("type", "todo")
                direction = self._infer_direction(speaker, target, commitment_type)
                assignee = target if direction == "outbound" else self.owner_speaker
                self.on_commitment_detected(PendingConfirmation(
                    task_text=c.get("text", ""),
                    speaker=speaker,
                    direction=direction,
                    assignee=assignee,
                    source_quote=raw_text[:500],
                    window_start=window_start,
                    window_end=window_end,
                    expires_at=time.time() + _CONFIRMATION_TTL,
                ))

    def _infer_direction(self, speaker: str, target: str | None, commitment_type: str) -> str:
        """promise 永遠 inbound（我自己要做）；todo 且 target 是別人 → outbound。"""
        if commitment_type == "promise":
            return "inbound"
        if speaker == self.owner_speaker and target and target != self.owner_speaker:
            return "outbound"
        return "inbound"

    @staticmethod
    def _format_transcript(utterances: list[dict]) -> str:
        lines = []
        for u in utterances:
            ts = datetime.datetime.fromtimestamp(u["timestamp"]).strftime("%H:%M:%S")
            lines.append(f"[{ts}] {u['speaker']}: {u['text']}")
        return "\n".join(lines)

    @staticmethod
    def _parse_llm_output(content: str) -> tuple[str, list[dict]]:
        try:
            data = json.loads(content)
            summary = data.get("summary", content)
            commitments = data.get("commitments", [])
            if not isinstance(commitments, list):
                commitments = []
            return summary, commitments
        except json.JSONDecodeError:
            return content, []
