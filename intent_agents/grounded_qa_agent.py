"""GroundedQAAgent — 喚醒 + 明確事實問句 → Gemini google_search grounded 回答（單發）。

design doc AmbientQA-20260830。GameKnowledgeAgent 的 sibling：同款 DeclarativeIntentAgent
+ regex bid + 貴呼叫留在 handler。差別是知識源走 Gemini 內建 google_search grounding
（真查證 + L1/L2 幻覺 guard），不是 Marvin 主 LLM 常識。

觸發（收斂版；backfill 2026-06→08 實測 3/3 精準、其餘 loose 命中多是閒聊反問）：
  A. 「查」動詞：馬文(幫我/幫忙/麻煩/請)? 查/查詢/查一下 X
  B. 事實問句尾：X 是什麼 / 是誰 / 叫什麼 / 什麼意思 / 怎麼做 / 有多少 …
  兩者都排除：點歌 / 找歌 / 歌詞、音量控制、問 Marvin 自身狀態、low_confidence_wake

不做：追問視窗、連續對談（走 /marvin_talk）、shadow gating（backfill 當 gate）。
付費鐵則：grounded 呼叫走 free→付費 key 鏈，付費過 PaidUsageGuard cap + 記帳
caller="ambient_qa"（feedback_paid_calls_must_record）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from intent_agents.base import (
    DeclarativeIntentAgent,
    IntentSchema,
    audio_rescue_slot,
    audio_rescue_slots_present,
    is_audio_rescue,
)
from intent_agents.constants import (
    MUSIC_PAUSE_KW, MUSIC_PLAY_KW, MUSIC_RESUME_KW, MUSIC_SKIP_KW, MUSIC_STOP_KW,
)
from intent_bus import IntentContext

logger = logging.getLogger(__name__)

AMBIENT_QA_LOG = Path("records/ambient_qa.jsonl")

# D3：不硬釘單一 model（feedback_llm_bus_model_staleness — Marmo 撞 Cerebras 全 404）。
MODEL_CHAIN: tuple[str, ...] = ("gemini-2.5-flash", "gemini-flash-latest")
GROUNDED_TIMEOUT_S = 8.0
MAX_REPLY_CHARS = 140

# ── 觸發 regex ────────────────────────────────────────────────────────────────

# A：明確「查」動詞（喚醒句裡，re.search 容忍 "馬文" 前綴）
_LOOKUP_RE = re.compile(r"(?:幫我|幫忙|麻煩|請|欸|快)?\s*查(?:詢|一下|查)?\s*(?P<topic>\S.{1,})")
# B：事實問句尾
_FACTUAL_TAIL_RE = re.compile(
    r"(?P<topic>.{2,}?)\s*"
    r"(?:是什麼|是誰|是哪(?:裡|一?個|一?國)?|叫什麼|什麼意思|意思是什麼|的意思"
    r"|怎麼做|怎麼用|怎麼弄|怎麼辦|有多少|是多少|多少錢|幾年|哪一年"
    r"|有?多[高大長遠重寬深久])"
    r"[啊呢喔嗎\s?？]*$"
)
_WAKE_PREFIX_RE = re.compile(r"^\s*(?:馬文|瑪文|麻文|媽文|marvin|marvy)\s*[，,、\s]*", re.IGNORECASE)

# ── 排除 ──────────────────────────────────────────────────────────────────────

_MUSIC_KW = tuple(set(MUSIC_PLAY_KW + MUSIC_SKIP_KW + MUSIC_STOP_KW
                      + MUSIC_PAUSE_KW + MUSIC_RESUME_KW))
_MUSIC_RE = re.compile("|".join(re.escape(k) for k in sorted(_MUSIC_KW, key=len, reverse=True)))
# constants 只收「搜尋歌曲」→ 補裸點歌動詞
_MUSIC_EXTRA_RE = re.compile(r"播放|放一?首|點一?首|^搜尋|唱一?首")
_VOLUME_RE = re.compile(r"大聲|小聲|音量|靜音|mute|volume", re.IGNORECASE)
# 找歌 / 歌詞（outside voice：「這首歌是什麼」含「什麼」會搶走 search_lyrics_grounded）
_FINDSONG_RE = re.compile(r"歌詞|這首歌|哪一?首|誰唱的|什麼歌|甚麼歌|這首是|這是哪")
# 問 Marvin 自身狀態（不可對外查證）
_SELF_RE = re.compile(r"^(你|妳|你們|馬文|自己)\b|你在(播|做|說|幹)|你(好|是誰|叫什麼|會不會)")

_HAN_RE = re.compile(r"[一-鿿]")


def _excluded(query: str) -> str | None:
    """回排除原因字串，或 None（沒被排除）。"""
    if _MUSIC_RE.search(query) or _MUSIC_EXTRA_RE.search(query):
        return "music"
    if _VOLUME_RE.search(query):
        return "volume"
    if _FINDSONG_RE.search(query):
        return "find_song"
    if _SELF_RE.search(query):
        return "self_referential"
    return None


def parse_grounded_qa(query: str) -> str | None:
    """喚醒句 → 要查的主題字串，或 None（不是明確事實問句）。

    純函式，無 I/O。給 GroundedQAAgent、backfill、測試共用。
    low_confidence_wake 由呼叫端另外擋（見 GroundedQAAgent.gate）。
    """
    q = _WAKE_PREFIX_RE.sub("", (query or "").strip()).strip()
    if not q:
        return None
    if _excluded(q):
        return None
    m = _LOOKUP_RE.search(q) or _FACTUAL_TAIL_RE.search(q)
    if not m:
        return None
    topic = (m.group("topic") or "").strip(" ，,、。.!！?？的")
    # 剝喚醒詞殘留
    topic = re.sub(r"^(馬文|瑪文|麻文|marvin)\s*", "", topic, flags=re.IGNORECASE).strip()
    if len(_HAN_RE.findall(topic)) < 2 and len(topic) < 3:
        return None
    return topic


# ── grounded 呼叫（~40 行，copy search_lyrics_grounded 骨架 + free→paid + guard）──

_SYSTEM_PROMPT = (
    "你是馬文。使用者在問一個事實問題（語音辨識來的，可能把數字 / 地名 / 專有名詞"
    "聽糊，例「台北一零一幾樓」可能是「台北 101 幾樓」）。先判斷他到底在問什麼，"
    "用 Google 查證，再用繁體中文口語、一兩句話直接給答案。關鍵資訊（日期 / 數字 / "
    "人名）寧可完整。不分點、不 markdown、不鋪陳。查不到就一行「無」。"
)
_REFUSAL_PREFIXES = ("無", "抱歉", "我不", "我沒辦法", "沒有這", "不清楚", "無法")


def _extract_sources(response) -> list[str]:
    try:
        cands = getattr(response, "candidates", None) or []
        gm = getattr(cands[0], "grounding_metadata", None) if cands else None
        chunks = getattr(gm, "grounding_chunks", None) or [] if gm else []
    except Exception:
        return []
    out = []
    for c in chunks[:4]:
        uri = getattr(getattr(c, "web", None) or c, "uri", "") or ""
        try:
            host = uri.split("/")[2] if "//" in uri else (uri[:40] or "?")
        except Exception:
            host = "?"
        out.append(host)
    return out


def _trim(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_REPLY_CHARS:
        return text
    head = text[:MAX_REPLY_CHARS]
    for i in range(len(head) - 1, 0, -1):
        if head[i] in "。！？!?…":
            return head[: i + 1]
    return head + "…"


async def grounded_answer(
    free_client,
    paid_client,
    guard,
    query: str,
    *,
    model_chain: tuple[str, ...] = MODEL_CHAIN,
    timeout: float = GROUNDED_TIMEOUT_S,
) -> tuple[str, list[str]] | None:
    """query → (答案, 來源 host 清單) 或 None（查不到 / 幻覺 guard 擋下 / 全失敗）。"""
    if not query or not query.strip():
        return None
    from google.genai import types
    from llm_paid import estimate_cost

    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        temperature=0.2,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )

    attempts: list[tuple[object, str, bool]] = []
    if free_client is not None:
        attempts.append((free_client, model_chain[0], False))
    est_in = 400
    # ⚠️ estimate_cost 只算 token；google_search grounding 另外 per-request 計費，
    # 所以 guard 的 daily cap 會低估真實花費（TODOS.md「PaidUsageGuard 低估 grounding」）。
    if paid_client is not None and guard.allow(estimate_cost(model_chain[0], est_in, 300)):
        attempts += [(paid_client, m, True) for m in model_chain]

    for client, model, is_paid in attempts:
        tier = "付費" if is_paid else "免費"
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(model=model, contents=query, config=config),
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(f"[AmbientQA] {tier} {model} 失敗：{str(exc)[:120]}，換下一個")
            continue

        if is_paid:
            usage = getattr(response, "usage_metadata", None)
            in_tok = int(getattr(usage, "prompt_token_count", 0) or est_in)
            out_tok = int(getattr(usage, "candidates_token_count", 0) or 300)
            guard.record(
                caller="ambient_qa", model=model, tokens=in_tok + out_tok,
                est_usd=estimate_cost(model, in_tok, out_tok),
                in_tokens=in_tok, out_tokens=out_tok,
            )

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            logger.info(f"[AmbientQA] {model} 空回應")
            continue
        try:
            fr = str(response.candidates[0].finish_reason)
            if "STOP" not in fr:
                logger.warning(f"[AmbientQA] {model} finish_reason={fr}（可能被截）")
        except Exception:
            pass

        first = text.splitlines()[0].strip()
        # L1：Gemini 自承查不到 / 拒答
        if first.startswith(_REFUSAL_PREFIXES):
            logger.info(f"[AmbientQA] L1 拒（LLM 回 {first[:20]!r}）query={query!r}")
            return None
        # L2：grounding_chunks 空 → 沒實際搜到網頁，疑似幻覺
        sources = _extract_sources(response)
        if not sources:
            logger.warning(f"[AmbientQA] L2 拒（grounding_chunks 空）query={query!r} ans={first[:40]!r}")
            return None

        logger.info(f"[AmbientQA] ✓ query={query!r} src={sources}")
        return _trim(text), sources

    return None


async def run_grounded_qa(ctrl, speaker: str, topic: str, *, raw: str = "") -> None:
    """handler 本體（放這裡而非 voice_controller，守 size budget 棘輪）。

    D7：ack 先出（貴呼叫前給「查詢中」提示，4.5s 死等會被讀成崩潰），再走 free→付費
    grounded 鏈。L1/L2 幻覺 guard 擋下就回「查不到」。用到的 ctrl 介面：_play_ack /
    play_tts / active_text_channel / stt_logger / bot.router。
    """
    import time as _time

    asyncio.create_task(ctrl._play_ack("status", speaker=speaker))

    router = getattr(ctrl.bot, "router", None)
    guard = getattr(ctrl, "_ambient_qa_guard", None)
    if guard is None:
        from llm_paid import PaidUsageGuard
        guard = PaidUsageGuard(daily_cap_usd=2.0, monthly_cap_usd=10.0)
        ctrl._ambient_qa_guard = guard

    t0 = _time.monotonic()
    res = None
    try:
        res = await grounded_answer(
            getattr(router, "google_client", None),
            getattr(router, "google_paid_client", None),
            guard, topic,
        )
    except Exception as e:
        logger.warning(f"[AmbientQA] grounded_answer 例外: {e}")
    latency_ms = int((_time.monotonic() - t0) * 1000)

    if res is None:
        ctrl.stt_logger.info(f"[BOT→{speaker}] (AmbientQA 查不到) {topic}")
        asyncio.create_task(ctrl.play_tts("這題我查不到。", already_in_channel=True))
        record_ambient_qa({"ts": _time.time(), "speaker": speaker, "raw": raw,
                           "query": topic, "answer": None, "reason": "no_answer",
                           "latency_ms": latency_ms})
        return

    answer, sources = res
    ctrl.stt_logger.info(f"[BOT→{speaker}] (AmbientQA) {answer}")
    asyncio.create_task(ctrl.play_tts(answer, already_in_channel=True))
    if ctrl.active_text_channel:
        src = f"\n_來源：{', '.join(sources)}_" if sources else ""
        asyncio.create_task(ctrl.active_text_channel.send(
            f"🔎 **【查詢】** `{speaker}`：{answer}{src}"))
    record_ambient_qa({"ts": _time.time(), "speaker": speaker, "raw": raw,
                       "query": topic, "answer": answer, "sources": sources,
                       "latency_ms": latency_ms, "downstream": "none"})
    logger.info(f"[AmbientQA] {speaker} 「{topic}」已回答（{latency_ms}ms, src={sources}）。")


def record_ambient_qa(rec: dict) -> None:
    """單次 write 整行 append（POSIX atomic for < PIPE_BUF）——不做 read-modify-write
    （music_memory.json 併發事故教訓，feedback_music_memory_concurrent_write_race）。"""
    try:
        AMBIENT_QA_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AMBIENT_QA_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[AmbientQA] 寫 log 失敗（忽略）：{e}")


# ── Agent ────────────────────────────────────────────────────────────────────

class GroundedQAAgent(DeclarativeIntentAgent):
    name = "grounded_qa"
    # 一般資訊請求，normal/stream 都活；game session 模式不出價（比照 GameKnowledgeAgent）。
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._cache is None:
            # 兩條 pattern 的 (?P<topic>…) named group 讓 regex 路徑自己填 topic slot；
            # 真正的觸發/排除判斷在 post_match_filter（parse_grounded_qa）。
            # required_slots=["topic"] 同時讓 audio-rescue manifest 把 topic 曝成
            # Gemini function 參數——糊字喚醒問句 → LLM 聽音訊把乾淨問題填進 topic。
            self._cache = [
                IntentSchema(
                    "factual_question", 0.75,
                    patterns=[_LOOKUP_RE.pattern, _FACTUAL_TAIL_RE.pattern],
                    required_slots=["topic"],
                    reason_template="ambient_qa",
                    manifest_description=(
                        "使用者在問一個需要查證的事實/常識問題（人事時地物、數字、"
                        "定義、怎麼做、某某是什麼/是誰）。把他要問的東西整理成通順的查詢"
                        "字串放進 topic。不是問正在播的歌、不是問對話歷史、不是點歌時用。"
                    ),
                ),
            ]
        return self._cache

    def gate(self, ctx: IntentContext) -> str | None:
        # audio-rescue：LLM 已聽過音訊才選中這個 intent，比 wake 信心啟發式強，不擋。
        if is_audio_rescue(ctx):
            return None
        if getattr(ctx, "low_confidence_wake", False):
            return "low_confidence_wake"
        return None

    def post_match_filter(self, schema, slots, ctx: IntentContext) -> bool:
        # audio-rescue：topic 由 LLM 從音訊填好，ctx.query 是糊掉的 STT，別再 re-parse。
        if is_audio_rescue(ctx):
            return audio_rescue_slots_present(slots, "topic")
        return parse_grounded_qa(ctx.query or "") is not None

    def make_handler(self, schema, slots, ctx: IntentContext):
        if is_audio_rescue(ctx):
            topic = audio_rescue_slot(slots, "topic", ctx)
        else:
            # regex 路徑：parse_grounded_qa 會剝喚醒詞前綴，比 raw named group 乾淨
            topic = parse_grounded_qa(ctx.query or "") or (ctx.query or "")

        async def _answer():
            await run_grounded_qa(self.ctrl, ctx.speaker, topic, raw=ctx.query or "")

        return _answer
