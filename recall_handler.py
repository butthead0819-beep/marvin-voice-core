"""
recall_handler.py

語音日記 Recall Pipeline：
  1. 待辦/承諾查詢 → task_store 直接回答
  2. 情境查詢 → summary_store 找窗口 → transcript_store 撈原話 → LLM 5W2H 合成
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PendingConfirmation:
    task_text: str
    speaker: str
    direction: str
    assignee: str
    source_quote: str
    window_start: float
    window_end: float
    expires_at: float  # unix timestamp，過期丟棄

_RECALL_PATTERNS = re.compile(
    r"剛才說|說了什麼|說過什麼|記得嗎|早上說|記得我說|忘了什麼"
    r"|答應了?什麼|有沒有答應|承諾了?什麼"
    r"|交辦了?什麼|交辦了?哪些|交辦給"
    r"|(?:剛才|早上|之前|昨天)提到"
    r"|待辦|任務清單|還有什麼事",
    re.IGNORECASE,
)

_TODO_PATTERNS = re.compile(r"待辦|要做|還有什麼事|任務清單", re.IGNORECASE)
_OUTBOUND_PATTERNS = re.compile(r"交辦|我叫.+去|我請.+去|我讓.+", re.IGNORECASE)
_COMMITMENT_PATTERNS = re.compile(r"答應|承諾|promise", re.IGNORECASE)
_MARK_DONE_PATTERNS = re.compile(
    r"做完|完成了|做好了|寄出|查好|買好|搞定了?|弄好|弄完了?|結束了|不用做了|算了不做"
    r"|處理好了?|辦好了?|解決了",
    re.IGNORECASE,
)

_MANUAL_ADD_PATTERNS = re.compile(
    r"記一下|幫我記|記住|記著|加一個待辦|待辦加",
    re.IGNORECASE,
)

_YES_PATTERNS = re.compile(
    r"^(對啊?|好[，,]?(?:記下去)?|是啊?|記下去|嗯+|沒錯|確認)[啊呢吧了。！!]?$",
    re.IGNORECASE,
)

_NO_PATTERNS = re.compile(
    # anchor `^...$` 對齊 _YES_PATTERNS，避免「我不要去」「對啊不是我說的」
    # 等含 no-word 的長句被誤當成 confirmation 的「不」（cross-context pollution）
    r"^(不用|不要|算了|不是|不對|取消記|不記)(記)?[啊呢吧了。！!]?$",
    re.IGNORECASE,
)

_TASK_UPDATE_PATTERNS = re.compile(
    r"改成|目標換|方向變|不是.+而是|換成",
    re.IGNORECASE,
)

_MANUAL_ADD_STRIP = re.compile(
    r"^(Marvin[,，]?\s*)?(記一下|幫我記[一下]?|記住|記著|加一個待辦|待辦加[一個]?)[,，]?\s*",
    re.IGNORECASE,
)

_UPDATE_STRIP = re.compile(
    r".*(改成|目標換成?|方向變成?|換成)\s*",
    re.IGNORECASE,
)

_TIMEOUT = 12.0
_GROQ_MODEL = "llama-3.1-8b-instant"

_5W2H_SYSTEM = """\
你是語音助理 Marvin。根據以下的對話摘要與原始對話記錄，用繁體中文簡短回答用戶的問題。
重點：直接引用原話（用引號標注），說明是誰說的、什麼時候說的（幾分鐘前）。
回答不超過 80 字。
"""


def is_recall_query(query: str) -> bool:
    return bool(_RECALL_PATTERNS.search(query))

def is_mark_done_query(query: str) -> bool:
    return bool(_MARK_DONE_PATTERNS.search(query))

def is_manual_add_query(query: str) -> bool:
    return bool(_MANUAL_ADD_PATTERNS.search(query))

def is_yes_response(query: str) -> bool:
    return bool(_YES_PATTERNS.search(query.strip()))

def is_no_response(query: str) -> bool:
    return bool(_NO_PATTERNS.search(query.strip()))

def is_task_update_query(query: str) -> bool:
    return bool(_TASK_UPDATE_PATTERNS.search(query))


class RecallHandler:
    def __init__(
        self,
        summary_store,
        task_store,
        transcript_store,
        groq_client,
        guild_id: int,
        owner_speaker: str,
    ):
        self.summary_store = summary_store
        self.task_store = task_store
        self.transcript_store = transcript_store
        self.groq_client = groq_client
        self.guild_id = guild_id
        self.owner_speaker = owner_speaker
        self.last_task_id: int | None = None  # 最近一次存入的 task id（供「那件事」解析）

    async def handle(self, speaker: str, query: str) -> str:
        # ── 路徑 A：待辦清單查詢 ──────────────────────────────────
        if _TODO_PATTERNS.search(query):
            direction = "outbound" if _OUTBOUND_PATTERNS.search(query) else None
            tasks = self.task_store.get_pending(guild_id=self.guild_id, direction=direction, speaker=speaker)

            # 如果有關鍵字，再用 search 縮小
            if tasks and not direction:
                keyword_hit = self._extract_search_keyword(query)
                if keyword_hit:
                    tasks = self.task_store.search(guild_id=self.guild_id, keyword=keyword_hit, speaker=speaker) or tasks

            if not tasks:
                return "目前沒有待辦事項，你都做完了或還沒有任何記錄。"

            lines = []
            for t in tasks[:5]:
                tag = "→ 我的" if t["direction"] == "inbound" else f"→ 交辦給 {t['assignee']}"
                lines.append(f"• {t['text']} {tag}")
            return "待辦清單：\n" + "\n".join(lines)

        # ── 路徑 B：承諾/交辦查詢 ────────────────────────────────
        if _COMMITMENT_PATTERNS.search(query) or _OUTBOUND_PATTERNS.search(query):
            direction = "outbound" if _OUTBOUND_PATTERNS.search(query) else "inbound"
            tasks = self.task_store.get_pending(guild_id=self.guild_id, direction=direction)
            if tasks:
                lines = [f"• {t['text']} (來自：「{t['source_quote'][:40]}...」)" for t in tasks[:3]]
                label = "交辦出去" if direction == "outbound" else "我答應的事"
                return f"{label}：\n" + "\n".join(lines)

        # ── 路徑 C：情境/對話內容查詢 → 摘要 + 原始 STT + LLM ────
        keyword = self._extract_search_keyword(query)
        if keyword:
            summaries = self.summary_store.search(guild_id=self.guild_id, keyword=keyword, hours=24)
        else:
            summaries = self.summary_store.get_summaries(guild_id=self.guild_id, hours=24)

        if not summaries:
            return "我找不到相關的對話記錄，可能是這個話題還沒有被記錄下來。"

        # 取最近 3 個相關窗口
        relevant = summaries[-3:]
        summary_context = "\n".join(
            f"[{_fmt_ago(s['window_end'])}] {s['summary_text']}" for s in relevant
        )

        # 撈最近 30 分鐘的原始 STT
        raw_utterances = self.transcript_store.get_recent(
            speaker=None, guild_id=self.guild_id, minutes=30
        )
        raw_text = "\n".join(
            f"[{_fmt_ago(u['timestamp'])}] {u['speaker']}: {u['text']}"
            for u in raw_utterances[-20:]
        )

        user_prompt = (
            f"用戶問題：「{query}」\n\n"
            f"對話摘要：\n{summary_context}\n\n"
            f"原始對話（細節）：\n{raw_text or '（無原始記錄）'}"
        )

        try:
            resp = await asyncio.wait_for(
                self.groq_client.chat.completions.create(
                    model=_GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": _5W2H_SYSTEM},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.2,
                    max_tokens=200,
                    stream=False,
                ),
                timeout=_TIMEOUT,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"[Recall] LLM 失敗，回傳摘要原文: {e}")
            # Fallback：直接回傳找到的摘要
            return f"我找到一些記錄：{relevant[-1]['summary_text']}"

    async def handle_mark_done(
        self, speaker: str, query: str, status: str = "done"
    ) -> str:
        """
        標記任務完成或取消。
        - 1 個 pending → 直接標記
        - keyword 命中唯一任務 → 標記
        - keyword 命中多個或無 keyword → 列出候選，請用戶說清楚
        - 0 個 pending → 友善提示
        """
        pending = self.task_store.get_pending(guild_id=self.guild_id, speaker=speaker)

        if not pending:
            return "目前沒有待辦事項可以標記完成。"

        # 嘗試用 query 關鍵字縮小範圍
        keyword = self._extract_mark_done_keyword(query)
        candidates = (
            self.task_store.search(guild_id=self.guild_id, keyword=keyword, speaker=speaker)
            if keyword else []
        )

        # 精確命中：keyword 搜尋只找到 1 個
        if len(candidates) == 1:
            t = candidates[0]
            self.task_store.update_status(t["id"], status)
            verb = "完成" if status == "done" else "取消"
            return f"已標記「{t['text']}」為{verb}。"

        # 只有 1 個 pending → 直接標記
        if len(pending) == 1:
            t = pending[0]
            self.task_store.update_status(t["id"], status)
            verb = "完成" if status == "done" else "取消"
            return f"已標記「{t['text']}」為{verb}。"

        # 多個候選 → 請用戶說清楚
        lines = [f"{i+1}. {t['text']}" for i, t in enumerate(pending[:5])]
        return "你說的是哪一件？\n" + "\n".join(lines)

    async def handle_manual_add(self, speaker: str, query: str) -> str:
        """語音關鍵字直接新增待辦，不等 SessionSummarizer 的 5 分鐘批次。"""
        task_text = _MANUAL_ADD_STRIP.sub("", query).strip()
        if not task_text:
            return "要記什麼？可以再說一次嗎？"
        now = time.time()
        self.last_task_id = self.task_store.save_task(
            guild_id=self.guild_id,
            text=task_text,
            direction="inbound",
            assignee=speaker,
            speaker=speaker,
            source_quote=query,
            source_window_start=now,
            source_window_end=now,
        )
        return f"好，記下來了：「{task_text}」"

    async def handle_task_update(
        self, speaker: str, query: str, last_task_id: int | None = None
    ) -> str:
        """更新已有任務的內容，不產生新任務。"""
        new_text = _UPDATE_STRIP.sub("", query).strip()
        if not new_text:
            return "要改成什麼？可以再說清楚一點嗎？"

        # 「那件事」→ 用 last_task_id
        if re.search(r"那件事|那個|那個任務", query) and last_task_id:
            self.task_store.update_text(last_task_id, new_text)
            return f"已更新：「{new_text}」"

        # 否則用 keyword 搜尋定位
        raw_kw = re.sub(r"改成.+|目標換.+|方向變.+|換成.+", "", query)
        keyword = self._extract_search_keyword(raw_kw)
        if keyword:
            keyword = re.sub(r"的事$|的$", "", keyword).strip() or keyword
        if keyword:
            candidates = self.task_store.search(
                guild_id=self.guild_id, keyword=keyword, speaker=speaker
            )
            if len(candidates) == 1:
                self.task_store.update_text(candidates[0]["id"], new_text)
                return f"已更新：「{new_text}」"
            if len(candidates) > 1:
                lines = [f"{i+1}. {t['text']}" for i, t in enumerate(candidates[:4])]
                return "找到多個符合的任務，你說的是哪一個？\n" + "\n".join(lines)

        return "找不到對應的任務，可以說清楚是哪一件事嗎？"

    async def handle_confirmation(self, conf: "PendingConfirmation") -> str:
        """將 PendingConfirmation 正式存入 task_store。"""
        self.task_store.save_task(
            guild_id=self.guild_id,
            text=conf.task_text,
            direction=conf.direction,
            assignee=conf.assignee,
            speaker=conf.speaker,
            source_quote=conf.source_quote,
            source_window_start=conf.window_start,
            source_window_end=conf.window_end,
        )
        return f"好，已記下：「{conf.task_text}」"

    @staticmethod
    def _extract_mark_done_keyword(query: str) -> str | None:
        stripped = re.sub(
            r"(做完|完成了|做好了|寄出|查好|買好|搞定|弄好|結束了|不用做了|取消|算了不做|了|好了)",
            "", query,
        ).strip()
        return stripped if len(stripped) >= 2 else None

    @staticmethod
    def _extract_search_keyword(query: str) -> str | None:
        # 移除常見的問句助詞，剩下的當關鍵字
        stripped = re.sub(
            r"我(剛才|早上|昨天|之前)?(說了?|提到|答應|交辦|叫|請|讓)?什麼?|"
            r"(剛才|早上|昨天|之前)?(說的|提到的|那件事|那個事)?|"
            r"(待辦|任務清單|還有什麼事|有沒有|記得嗎)",
            "", query,
        ).strip()
        return stripped if len(stripped) >= 2 else None


def _fmt_ago(ts: float) -> str:
    mins = int((time.time() - ts) / 60)
    if mins < 1:
        return "剛才"
    if mins < 60:
        return f"{mins} 分鐘前"
    return f"{mins // 60} 小時前"
