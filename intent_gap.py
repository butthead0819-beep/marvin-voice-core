"""Intent gap detection — bus 沒贏家 + 有 intent 訊號時的紀錄 + ack 流程。

Phase A 範圍：只負責「偵測 gap → 寫 records/agent_gaps.jsonl → 播模板 ack」。
不嘗試完成任務（那是 Phase C，要 openclaw tool calling）。

模組分工（之後步驟逐步加入此檔）：
- IntentGapRecord  ← 本檔，schema dataclass
- build_intent_manifest()  ← 步驟 2
- gap_classifier()         ← 步驟 3
- gap_logger / dedup       ← 步驟 4
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from intent_bus import IntentContext


logger = logging.getLogger("cogs.voice_controller.intent_gap")


@dataclass(frozen=True)
class IntentGapRecord:
    """有 intent 訊號但無 agent 命中的紀錄。寫入 records/agent_gaps.jsonl。

    schema_version=1 從第一筆 record 算起 — 未來 consumer（daily ritual /
    Claude Code 補 agent）演進欄位時，沒版本欄就只能猜。

    UNKNOWN intent_type 是 classifier failure 的合法狀態：保留 raw_query
    給 daily ritual 看，但不播 ack（避免講錯話）。
    """
    utterance_id: str
    ts: float
    speaker: str
    mode: str
    raw_query: str
    cleaned_query: str
    intent_type: str  # classifier 失敗時為 "UNKNOWN"
    slots: dict[str, Any]
    nearest_agent: str | None
    nearest_distance: float | None
    ack_text: str | None  # None = 沒播 ack（UNKNOWN / 5min dedup skipped）
    acknowledged: bool
    schema_version: int = 1

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IntentGapRecord:
        return cls(**d)


# ─────────────────────────────────────────────────────────────────────────────
# gap_classifier — cheap LLM 把「沒命中 agent 的 query」歸類成 gap record。
# 沿用 TieredLLMRouter.quick（同 chat_classifier_judge 的設施）。
# ─────────────────────────────────────────────────────────────────────────────

GapClassifierCall = Callable[[str, dict], Awaitable[dict]]


_GAP_SYSTEM_PROMPT = """你是 Discord 語音 bot 的 intent gap classifier。
使用者說了一句話，IntentBus 沒有 agent 命中。請判讀：
1. intent_type — 描述使用者「真正想要什麼」的 snake_case 標籤（如 replay_user_history /
   change_voice / game_knowledge_query）。
   **重要：intent_type 必須描述實際需求本身，禁止直接借用 available_agents 裡的 agent
   名稱。**「query 最接近哪個現有 agent」是 nearest_agent 欄位的事，兩者分開。
   intent_type 跟著「主題對象」走，不是跟著動詞。同樣是「查」，主題不同 intent_type 不同：
   - 「幫我查這首歌的歌詞」→ 主題是歌 → intent_type=song_lyrics_lookup
   - 「幫我查麥塊鑽石去哪挖」→ 主題是遊戲知識 → intent_type=game_knowledge_query
   （nearest_agent 可填語意最近的現有 agent，但 intent_type 絕不可直接是 agent 名）。
   若這句話本身無意圖（閒聊／反問／雜訊）→ "UNKNOWN"
2. slots — 從 query 抽取的關鍵欄位 dict（例如 {"target_user": "showay"}）
3. nearest_agent — available_agents 裡語意最接近的 name；找不到回 null
4. nearest_distance — 與最接近 agent 的距離 0.0–1.0（0=完美 match；1=完全不相關）；找不到回 null
5. ack_text — 回給使用者的一句自然口語繁體中文 ≤ 30 字，告知意圖已收到但功能還在開發；
   intent_type=UNKNOWN 時回 null（不該對雜訊播 ack）

輸出 JSON only：
{"intent_type": str, "slots": dict, "nearest_agent": str|null, "nearest_distance": float|null, "ack_text": str|null}"""


_GAP_SAFE_DEFAULT: dict = {
    "intent_type": "UNKNOWN",
    "slots": {},
    "nearest_agent": None,
    "nearest_distance": None,
    "ack_text": None,
}


def make_groq_gap_classifier(router) -> GapClassifierCall:
    """Build a GapClassifierCall bound to the given TieredLLMRouter's quick tier.

    Failure modes（拍板 #5）：
    - router.quick 回 None（pool 全冷）→ 回 safe default UNKNOWN（caller 不會炸）
    - JSON 解析失敗 → raise（caller 接住並寫 UNKNOWN gap、不播 ack）
    """

    async def _call(cleaned_query: str, manifest: dict) -> dict:
        # 只塞 agents 陣列；version 是 cache key，LLM 看不到也沒差，省 token。
        manifest_summary = json.dumps(manifest["agents"], ensure_ascii=False)
        prompt = f'query="{cleaned_query}"\navailable_agents={manifest_summary}'
        response = await router.quick(
            prompt=prompt,
            caller="gap_classifier",
            system=_GAP_SYSTEM_PROMPT,
            max_tokens=200,
            temperature=0.0,
            json=True,
        )
        if response is None:
            return dict(_GAP_SAFE_DEFAULT)
        return json.loads(response)

    return _call


# ─────────────────────────────────────────────────────────────────────────────
# GapLogger — append-only JSONL writer + in-memory (speaker, intent_type) ack
# dedup cache。Process restart 會清空 cache（5min 視窗短，可接受）。
# JSONL 永遠寫（包含 UNKNOWN / dedup-skipped record，daily ritual 才看得到頻率）。
# ─────────────────────────────────────────────────────────────────────────────


class GapLogger:
    def __init__(self, jsonl_path: Path | str, dedup_window_s: float = 300.0):
        self.jsonl_path = Path(jsonl_path)
        self.dedup_window_s = dedup_window_s
        self._last_ack: dict[tuple[str, str], float] = {}

    def write(self, record: IntentGapRecord) -> None:
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(record.to_jsonl() + "\n")

    def should_ack(self, speaker: str, intent_type: str, now: float) -> bool:
        """同 (speaker, intent_type) 5min 內已 ack 過 → False。caller 自己保證
        UNKNOWN 不該叫這個。"""
        last = self._last_ack.get((speaker, intent_type))
        if last is None:
            return True
        return (now - last) >= self.dedup_window_s

    def mark_acked(self, speaker: str, intent_type: str, now: float) -> None:
        self._last_ack[(speaker, intent_type)] = now
        # 順手清超過 2x window 的 stale entry，避免 dict 無限長大。
        cutoff = now - self.dedup_window_s * 2
        self._last_ack = {k: v for k, v in self._last_ack.items() if v >= cutoff}


# ─────────────────────────────────────────────────────────────────────────────
# handle_intent_gap — Phase A orchestrator。Caller (voice_controller) 在 bus
# dispatch 沒贏家 + has_intent_signal=true 時呼叫，永遠寫 JSONL；只在
# (intent_type != UNKNOWN) + (LLM 給 ack_text) + (5min dedup pass) 時播 TTS。
# ─────────────────────────────────────────────────────────────────────────────


async def handle_intent_gap(
    ctx: IntentContext,
    *,
    utterance_id: str,
    classifier: GapClassifierCall,
    gap_logger: GapLogger,
    manifest: dict,
    tts_call: Callable[[str], Awaitable[None]],
) -> IntentGapRecord:
    """Return 寫入的 record — caller 看 intent_type 決定要不要 fall through 到 Marvin。"""
    try:
        result = await classifier(ctx.query, manifest)
    except Exception as exc:
        logger.warning(
            f"⚠️ [IntentGap] classifier 炸了，寫 UNKNOWN gap 不播 ack: {exc}"
        )
        result = dict(_GAP_SAFE_DEFAULT)

    intent_type = result.get("intent_type") or "UNKNOWN"
    slots = result.get("slots") or {}
    nearest_agent = result.get("nearest_agent")
    nearest_distance = result.get("nearest_distance")
    llm_ack_text = result.get("ack_text")

    should_play = bool(
        intent_type != "UNKNOWN"
        and llm_ack_text
        and gap_logger.should_ack(ctx.speaker, intent_type, ctx.now)
    )

    record = IntentGapRecord(
        utterance_id=utterance_id,
        ts=ctx.now,
        speaker=ctx.speaker,
        mode=ctx.mode,
        raw_query=ctx.raw_text,
        cleaned_query=ctx.query,
        intent_type=intent_type,
        slots=slots,
        nearest_agent=nearest_agent,
        nearest_distance=nearest_distance,
        ack_text=llm_ack_text if should_play else None,
        acknowledged=should_play,
    )
    gap_logger.write(record)

    if should_play:
        gap_logger.mark_acked(ctx.speaker, intent_type, ctx.now)
        try:
            await tts_call(llm_ack_text)
        except Exception as exc:
            logger.warning(f"⚠️ [IntentGap] ack TTS 播放失敗（忽略）: {exc}")

    return record
