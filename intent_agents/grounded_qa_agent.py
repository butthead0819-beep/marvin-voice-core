"""GroundedQAAgent — 喚醒 + 明確事實問句 → Gemini google_search grounded 回答（單發）。

design doc AmbientQA-20260830。GameKnowledgeAgent 的 sibling：同款 DeclarativeIntentAgent
+ regex bid + 貴呼叫留在 handler。差別是知識源走 Gemini 內建 google_search grounding
（真查證 + L1/L2 幻覺 guard），不是 Marvin 主 LLM 常識。

觸發（收斂版；backfill 2026-06→08 實測 3/3 精準、其餘 loose 命中多是閒聊反問）：
  A. 查詢動詞：馬文(幫我/幫忙/麻煩/請)? 查/查詢/查一下/搜尋/請問/想知道/找一下 X
     （寬版疑問詞 / 句尾嗎呢 9/19 試過又拿掉：「今天晚餐吃什麼」「我帥嗎」全被搶去查）
  B. 事實問句尾：X 是什麼 / 是誰 / 叫什麼 / 什麼意思 / 怎麼做 / 有多少 …
  兩者都排除：點歌 / 找歌 / 歌詞、音量控制、問 Marvin 自身狀態、low_confidence_wake

不做：追問視窗、連續對談、shadow gating（backfill 當 gate）。
付費鐵則：grounded 呼叫走 free→付費 key 鏈，付費過 PaidUsageGuard cap + 記帳
caller="ambient_qa"（feedback_paid_calls_must_record）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
import pipeline_timing
import recent_games
import game_aliases
import time as _time
import turn_outcome

logger = logging.getLogger(__name__)

AMBIENT_QA_LOG = Path("records/ambient_qa.jsonl")

# D3：不硬釘單一 model（feedback_llm_bus_model_staleness — Marmo 撞 Cerebras 全 404）。
MODEL_CHAIN: tuple[str, ...] = ("gemini-2.5-flash", "gemini-3.5-flash-lite")  # 10/23 拿掉 -latest 別名（曾被計成 3.8-flash）
GROUNDED_TIMEOUT_S = 8.0
MAX_REPLY_CHARS = 140

# 喚醒查詢：3.5-flash-lite 先上（10/23 實測 14 題 11 題答出、2.1s、成本約 1/18），答不出／被 L1/L2 擋換 3.5-flash（13/14、6.5s），再備援 2.5-flash。
QA_MODEL_CHAIN: tuple[str, ...] = ("gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash")
QA_TIMEOUT_S = 15.0

# Gemini 3.x grounding 按「模型實際執行的搜尋次數」計費（$14/1000，超過每月免費額度後）；
# 2.5 按 prompt 計但每天 1500 免費。免費額度內不計費（見下行）。
GROUNDING_USD_PER_SEARCH = 0.0  # 2026-10-23：Gemini 3+ 每月前 5000 次搜尋免費（共用），Marvin 月用量 <1000；超過再改回 0.014

# 對談查詢前先「整理問題」（2026-10-23 拍板）：語音問題常聽糊、省略主詞（「光點二代」＝
# Grounded 2、「是二代還是一代」接著前一輪）。
# 10/23 重評（正確標準答案：「幫註」也是 Grounded 2，不是 Brotato）：LOW 思考中位數 1.3s／
# 預設 3.2s，12 題整理結果相當；「幫註」各檔位都猜成 Brotato，要靠誤聽對照表
# （docs/designs/game-alias-table.md）。
REWRITE_MODEL = "gemini-3.5-flash"
REWRITE_TIMEOUT_S = 8.0
READBACK_MAX_CHARS = 40
_REWRITE_SYSTEM_PROMPT = (
    "你是馬文的「問題整理」：使用者用語音問問題，語音辨識常把專有名詞、遊戲名、數字聽糊，"
    "句子也常省略主詞或接著前面的話。你的工作只有一件：根據提供的資料，把【使用者原句】整理"
    "成一句「不看上下文也看得懂、可以直接拿去 Google 搜尋」的繁體中文問題。規則：1. 只用資料"
    "裡有根據的資訊補主詞、指稱、遊戲名稱與版本、修正聽糊的詞；沒根據就不要補，不要自己發明。"
    "2. 保留使用者真正想問的事，不要改成別的問題、不要加問他沒問的事。3. 如果原句其實不是問題"
    "（感嘆、聊天、在跟別人講話），question 填空字串。4. 只輸出 JSON：{\"question\": \"整理後的"
    "問題\", \"basis\": \"用了哪些資料（簡短）\"}"
)
_READBACK_STRIP_RE = re.compile(r"[，。、？?！!：:「」『』（）() ]")

# ── 觸發 regex ────────────────────────────────────────────────────────────────

# A：明確「查」/「搜尋」/「問」動詞——只認句首（可帶喚醒詞、我/你、幫我…）。
# 10/21：re.search 任意位置會把「我進入了那個輪巡查詢之後還可以嗎」的句中「查詢」當指令；
# 句中提到的交給 try_question_opener（3.5-flash 判斷，「略」交回聊天）。
_LOOKUP_RE = re.compile(
    r"^\s*(?:(?:馬文|瑪文|麻文|媽文|marvin|marvy)\s*[，,、\s]*)?"
    r"(?:我|你|妳)?\s*(?:幫我|幫忙|麻煩|請|欸|快)?\s*"
    r"(?:查(?:詢|一下|查)?|搜尋|查查|問一下|請問|想問|想知道|知不知道|找一下|找找)\s*"
    r"(?P<topic>\S.{1,})",
    re.IGNORECASE,
)
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
# 問 Marvin 自身狀態或人格聊天（不可對外查證，保留走 Marvin 主 LLM 閒聊）
_SELF_RE = re.compile(
    r"^(?:你|妳|你們|自己)\s*(?:覺得|認為|喜歡|討厭|怕|愛|想|會|能不能|可不可以|要不要|想不想|是誰|叫什麼|好帥|真帥|好美|很棒|好不好看)"
    r"|^(?:你好|您好|嗨|哈囉|早安|午安|晚安)"
    r"|你在(?:播|做|說|幹|聽|看)"
    r"|你(?:好|是誰|叫什麼|會|能不能|可不可以|要不要|想不想|覺得|認為|喜歡|討厭|愛|怕)"
)

_HAN_RE = re.compile(r"[一-鿿]")
# session 追問黏著 grounded 的額外排除：口語點歌（「播晴天」「那就播這首」「聽聽看第一首」「放五月天」）
# 沒有 music agent 出價、_excluded 也不擋 → 不排除就會被當查詢丟去 Google。
# 「放」只認後接點歌語境，避免誤擋「他放了幾張專輯」。
_SESSION_PLAY_RE = re.compile(r"播|放(?:一|這|那|第|來|首|吧|個|點)|^放(?!假|心|棄|學|榜|過|了)|來聽|聽聽|點(?:一|這|那|第)?首|唱")

# session 黏著只接看起來像問句的話；「那就是你的問題了」「就自然不是有的」這種評論/跟旁人說話不送 Google
# （10/21 一個 session 4 句被硬查成「這題我查不到」）。漏判的交給 _SESSION_SYSTEM_PROMPT 的「略」。
_SESSION_QUESTION_RE = re.compile(
    r"[?？]|嗎|呢|什麼|甚麼|啥|怎麼|怎樣|如何|為什麼|為何|哪|誰|幾|多少"
    r"|是不是|有沒有|能不能|可不可以|會不會|要不要|還是|知道|告訴我|查|找|介紹"
)


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
    """喚醒句 → 要查的主題字串，或 None（不是明確問句或被排除）。

    純函式，無 I/O。給 GroundedQAAgent、backfill、測試共用。
    low_confidence_wake 由呼叫端另外擋（見 GroundedQAAgent.gate）。
    """
    q = _WAKE_PREFIX_RE.sub("", (query or "").strip()).strip()
    if not q:
        return None
    if _excluded(q):
        return None

    topic: str | None = None
    m = _LOOKUP_RE.search(q)
    if m:
        topic = (m.group("topic") or "").strip(" ，,、。.!！?？的")
    else:
        m_tail = _FACTUAL_TAIL_RE.search(q)
        if m_tail:
            topic = (m_tail.group("topic") or "").strip(" ，,、。.!！?？的")

    if not topic:
        return None

    # 剝喚醒詞殘留
    topic = re.sub(r"^(馬文|瑪文|麻文|marvin)\s*", "", topic, flags=re.IGNORECASE).strip()
    if len(_HAN_RE.findall(topic)) < 2 and len(topic) < 3:
        return None
    return topic


# ── grounded 呼叫（~40 行，copy search_lyrics_grounded 骨架 + free→paid + guard）──

_SYSTEM_PROMPT = (
    "你是馬文。使用者在問一個事實、生活、時事或知識問題（語音辨識來的，可能把數字 / 地名 / 專有名詞"
    "聽糊，例「台北一零一幾樓」可能是「台北 101 幾樓」）。先判斷他到底在問什麼，"
    "若有提供【過去 10 分鐘語音對話背景】，請結合對話背景理解指稱（例「那款」、「剛剛說的」、「那家店」）。"
    "用 Google 查證，再用繁體中文口語、一兩句話直接給答案。關鍵資訊（日期 / 數字 / "
    "人名）寧可完整。不分點、不 markdown、不鋪陳。查不到就一行「無」。"
)
_REFUSAL_PREFIXES = ("無", "抱歉", "我不", "我沒辦法", "沒有這", "不清楚", "無法")

_SESSION_SYSTEM_PROMPT = _SYSTEM_PROMPT + (
    "這是連續對談中的一句話：如果【使用者當前提問】其實不是要你查的問題（評論、抱怨、感嘆、"
    "在跟旁邊的人講話、問你馬文自己的狀態），不要查、不要回答，只回一個字「略」。"
)
_OPENER_SYSTEM_PROMPT = _SYSTEM_PROMPT + (
    "使用者叫了你的名字說這句話：如果它不是需要查證的事實、知識、時事或攻略問題（閒聊、問你的意見或心情、"
    "開玩笑、在跟旁邊的人講話），不要查、不要回答，只回一個字「略」。"
)
# 10/23：來源可能是 Discord 當下或最近 7 天玩過（recent_games）→ 只當參考。13 題實測：攻略題答對版本，
# 但「是二代還是一代」會被帶成二代；強制／軟性措辭結果相同，選軟性是因為來源不一定是「正在玩」。
_GAME_HINT_LABEL = "【大家最近常玩的遊戲（僅供參考：問題明顯在講遊戲、對話背景又沒指出別款時才套用；跟遊戲無關就忽略）】"
NOT_A_QUESTION = object()   # grounded_answer 回這個＝模型判定不是問題（只有 session prompt 會產生）

_NEXT = object()   # _attempt 內部哨兵：這次嘗試失敗（例外/空回應）→ 換下一個


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


def _count_searches(response) -> int:
    try:
        cands = getattr(response, "candidates", None) or []
        gm = getattr(cands[0], "grounding_metadata", None) if cands else None
        qs = getattr(gm, "web_search_queries", None) if gm else None
        return len(qs) if isinstance(qs, (list, tuple)) else 0
    except Exception:
        return 0


def _trim(text: str, max_chars: int = MAX_REPLY_CHARS) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    for i in range(len(head) - 1, 0, -1):
        if head[i] in "。！？!?…":
            return head[: i + 1]
    return head + "…"


def current_games(ctrl, speaker: str) -> list[str]:
    """語音頻道裡大家正在玩的遊戲（Discord Rich Presence），說話者排最前。

    純函式風格（只讀 ctrl），任何一層缺失都安全降級回 []。
    """
    try:
        import discord

        members = ctrl.voice_client.channel.members
        speaker_games: list[str] = []
        other_games: list[str] = []
        present: list[str] = []
        for m in members:
            if getattr(m, "bot", False):
                continue
            names: list[str] = []
            for act in (getattr(m, "activities", None) or []):
                if getattr(act, "type", None) == discord.ActivityType.playing:
                    name = (getattr(act, "name", None) or "").strip()
                    if name:
                        names.append(name)
            if getattr(m, "display_name", None) == speaker:
                speaker_games.extend(names)
            else:
                other_games.extend(names)
            present.append(getattr(m, "display_name", "") or "")
            if names:
                recent_games.note_games(present[-1], names, now=_time.time())

        seen: set[str] = set()
        out: list[str] = []
        for name in speaker_games + other_games:
            if name not in seen:
                seen.add(name)
                out.append(name)
            if len(out) >= 3:
                break
        # 當下抓不到（PS 上玩 Discord 不顯示）→ 在場的人最近 7 天玩過的（見 recent_games.py）
        return out or recent_games.recent_games(present, now=_time.time())
    except Exception:
        return []


async def build_recent_transcript_context(
    ctrl,
    guild_id: int | None = None,
    minutes: int = 10,
    max_entries: int = 40,
    max_chars: int = 2000,
) -> str:
    """自 TranscriptStore 異步撈取頻道過去 N 分鐘對話，組裝為簡潔上下文。

    若 store 缺失或發生例外，回傳空字串（安全降級，不影響查詢）。
    """
    store = getattr(ctrl, "_transcript_store", None)
    if store is None:
        store = getattr(getattr(ctrl, "bot", None), "_transcript_store", None)
    if store is None:
        return ""

    if guild_id is None:
        effective_guild = 0
        act_chan = getattr(ctrl, "active_text_channel", None)
        if act_chan and getattr(act_chan, "guild", None):
            effective_guild = getattr(act_chan.guild, "id", 0)
        elif getattr(ctrl, "guild_id", None):
            effective_guild = ctrl.guild_id
    else:
        effective_guild = guild_id

    try:
        rows = await asyncio.to_thread(store.get_recent, guild_id=effective_guild, minutes=minutes)
        if not rows and effective_guild != 0:
            rows = await asyncio.to_thread(store.get_recent, guild_id=0, minutes=minutes)
    except Exception as e:
        logger.warning(f"[AmbientQA] 取得近期 transcript 失敗（忽略）：{e}")
        return ""

    if not rows:
        return ""

    valid_entries = []
    for r in rows:
        text = (r.get("text") or "").strip()
        speaker = (r.get("speaker") or "未知").strip()
        if len(text) >= 2:
            valid_entries.append(f"{speaker}: {text}")

    if not valid_entries:
        return ""

    tail = valid_entries[-max_entries:]
    combined = "\n".join(tail)
    if len(combined) > max_chars:
        combined = combined[-max_chars:]
        nl_idx = combined.find("\n")
        if nl_idx != -1:
            combined = combined[nl_idx + 1:]

    return combined


def build_rewrite_input(
    query: str,
    *,
    speaker: str,
    recent_context: str,
    session_history: list[tuple[str, str]],
    games: list[str],
    alias_hint: str = "",
) -> str:
    """純函式：組「問題整理」送給 Gemini 的 contents 字串。段落順序固定、空段落略過。"""
    segments: list[str] = []
    if alias_hint:
        segments.append(alias_hint)
    if games:
        segments.append(f"【大家最近在玩的遊戲】{'、'.join(games)}")
    if recent_context and recent_context.strip():
        segments.append(f"【過去 10 分鐘語音對話背景】\n{recent_context.strip()}")
    if session_history:
        qa_lines = "\n".join(f"問：{q}\n答：{a}" for q, a in session_history)
        segments.append(f"【這次對話 Marvin 剛回答過的問答】\n{qa_lines}")
    segments.append(f"【提問人】{speaker}\n【使用者原句】{query.strip()}")
    return "\n\n".join(segments)


async def rewrite_question(
    paid_client,
    guard,
    query: str,
    *,
    speaker: str,
    recent_context: str,
    session_history: list[tuple[str, str]],
    games: list[str],
    alias_hint: str = "",
) -> str | None:
    """把語音聽糊、省略主詞的原句整理成一句獨立明確的問題；None＝失敗或關閉（用原句查）。"""
    if paid_client is None or os.environ.get("MARVIN_QA_REWRITE", "1") == "0":
        return None
    from google.genai import types
    from llm_paid import estimate_cost

    if not guard.allow(estimate_cost(REWRITE_MODEL, 1500, 300)):
        return None

    t0 = _time.monotonic()
    contents = build_rewrite_input(
        query, speaker=speaker, recent_context=recent_context,
        session_history=session_history, games=games, alias_hint=alias_hint,
    )
    try:
        response = await asyncio.wait_for(
            paid_client.aio.models.generate_content(
                model=REWRITE_MODEL, contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=_REWRITE_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                    thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                ),
            ),
            timeout=REWRITE_TIMEOUT_S,
        )
        usage = getattr(response, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
        guard.record(
            caller="qa_rewriter", model=REWRITE_MODEL, tokens=in_tok + out_tok,
            est_usd=estimate_cost(REWRITE_MODEL, in_tok, out_tok),
            in_tokens=in_tok, out_tokens=out_tok,
        )
        question = json.loads(response.text)["question"].strip()
    except Exception as e:
        logger.warning(f"[AmbientQA] 問題整理失敗（用原句查）：{e}")
        return None

    ms = int((_time.monotonic() - t0) * 1000)
    logger.info(f"[AmbientQA] 整理 {query!r} ⇒ {question!r}（{ms}ms）")
    return question


def readback_text(query: str, rewritten: str) -> str | None:
    """整理後的問題讀回句，或 None（不讀回：跟原句沒差、或太長）。"""
    if _READBACK_STRIP_RE.sub("", query).strip() == _READBACK_STRIP_RE.sub("", rewritten).strip():
        return None
    if len(rewritten) > READBACK_MAX_CHARS:
        return None
    return f"你是問{rewritten.rstrip('？?。')}嗎？我查一下。"


async def grounded_answer(
    free_client,
    paid_client,
    guard,
    query: str,
    *,
    recent_context: str = "",
    game_context: list[str] | None = None,
    session_history: list[tuple[str, str]] | None = None,
    model_chain: tuple[str, ...] = MODEL_CHAIN,
    timeout: float = GROUNDED_TIMEOUT_S,
    system_prompt: str = _SYSTEM_PROMPT,
    caller: str = "ambient_qa",
    max_chars: int = MAX_REPLY_CHARS,
    alias_hint: str = "",
    meta: dict | None = None,
) -> tuple[str, list[str]] | object | None:
    """query → (答案, 來源 host 清單) 或 None（查不到 / 幻覺 guard 擋下 / 全失敗）或 NOT_A_QUESTION
    （session prompt 判定這句其實不是要查的問題）。"""
    if not query or not query.strip():
        return None
    from google.genai import types
    from llm_paid import estimate_cost

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.2,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )

    segments: list[str] = []
    if alias_hint:
        segments.append(alias_hint)
    if game_context:
        segments.append(
            f"{_GAME_HINT_LABEL}{'、'.join(game_context)}"
        )
    if recent_context and recent_context.strip():
        segments.append(
            f"【過去 10 分鐘語音對話背景（僅供參考指稱與上下文脈絡，勿逐句回答）】\n"
            f"{recent_context.strip()}"
        )
    if session_history:
        qa_lines = "\n".join(f"問：{q}\n答：{a}" for q, a in session_history)
        segments.append(
            f"【這次對話你剛回答過的問答（使用者說「這個 / 那個 / 那裡」多半指這裡提到的東西）】\n"
            f"{qa_lines}"
        )
    if segments:
        segments.append(f"【使用者當前提問】\n{query.strip()}")
        contents = "\n\n".join(segments)
    else:
        contents = query.strip()

    est_in = 400 + (len(recent_context) // 2 if recent_context else 0)
    est_in += sum(len(q) + len(a) for q, a in (session_history or [])) // 2

    async def _attempt(client, model: str, is_paid: bool):
        tier = "付費" if is_paid else "免費"
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(model=model, contents=contents, config=config),
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(f"[AmbientQA] {tier} {model} 失敗：{str(exc)[:120]}，換下一個")
            if meta is not None:
                meta.setdefault("tried", []).append(f"{model}{tier}✗")
            return _NEXT

        if meta is not None:
            meta.update(model=model, paid=is_paid, searches=_count_searches(response))

        if is_paid:
            usage = getattr(response, "usage_metadata", None)
            in_tok = int(getattr(usage, "prompt_token_count", 0) or est_in)
            # candidates_token_count 不含思考 token（2.5/3.x 預設會思考）→ 要加 thoughts_token_count，否則帳本低估
            out_tok = int(getattr(usage, "candidates_token_count", 0) or 300) + int(getattr(usage, "thoughts_token_count", 0) or 0)
            n_search = _count_searches(response)
            guard.record(
                caller=caller, model=model, tokens=in_tok + out_tok,
                est_usd=estimate_cost(model, in_tok, out_tok) + n_search * GROUNDING_USD_PER_SEARCH,
                in_tokens=in_tok, out_tokens=out_tok,
            )

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            logger.info(f"[AmbientQA] {model} 空回應")
            if meta is not None:
                meta.setdefault("tried", []).append(f"{model}{tier}✗")
            return _NEXT
        try:
            fr = str(response.candidates[0].finish_reason)
            if "STOP" not in fr:
                logger.warning(f"[AmbientQA] {model} finish_reason={fr}（可能被截）")
        except Exception:
            pass

        first = text.splitlines()[0].strip()
        if first.rstrip("。.!！ ") == "略":
            logger.info(f"[AmbientQA] 判定非問題（略）query={query!r}")
            return NOT_A_QUESTION
        # L1：Gemini 自承查不到 / 拒答
        if first.startswith(_REFUSAL_PREFIXES):
            logger.info(f"[AmbientQA] L1 拒（LLM 回 {first[:20]!r}）query={query!r}")
            return _NEXT   # 換下一個模型試（lite 答不出換大模型）；鏈尾仍回 None
        # L2：grounding_chunks 空 → 沒實際搜到網頁，疑似幻覺
        sources = _extract_sources(response)
        if not sources:
            logger.warning(f"[AmbientQA] L2 拒（grounding_chunks 空）query={query!r} ans={first[:40]!r}")
            return _NEXT   # 同上

        logger.info(f"[AmbientQA] ✓ query={query!r} src={sources}")
        return _trim(text, max_chars), sources

    async def _chain(atts: list[tuple[object, str, bool]]):
        for client, model, is_paid in atts:
            r = await _attempt(client, model, is_paid)
            if r is not _NEXT:
                return r
        return _NEXT

    free_atts: list[tuple[object, str, bool]] = []
    if free_client is not None:
        free_atts.append((free_client, model_chain[0], False))
    paid_atts: list[tuple[object, str, bool]] = []
    # guard.allow 預估只算 token；實際記帳（_attempt）有加搜尋費。
    if paid_client is not None and guard.allow(estimate_cost(model_chain[0], est_in, 300)):
        paid_atts = [(paid_client, m, True) for m in model_chain]

    r = await _chain(free_atts + paid_atts)
    return None if r is _NEXT else r


def format_provenance(meta: dict, latency_ms: int, n_history: int, n_context: int, games: list[str]) -> str:
    """[BOT→] 標籤：這句回答用了哪個模型、付不付費、搜尋幾次、帶了多少上下文。"""
    parts = []
    if meta.get("tried"):
        parts.append(" ".join(meta["tried"]) + " →")
    if meta.get("model"):
        parts.append(f"{meta['model']}{'付費' if meta.get('paid') else '免費'}")
        parts.append(f"搜尋{meta.get('searches', 0)}次")
    else:
        parts.append("全部失敗")
    parts.append(f"{latency_ms / 1000:.1f}s")
    ctx = f"上下文:問答{n_history}輪/背景{n_context}句"
    if games:
        ctx += f"/遊戲{'、'.join(games)}"
    parts.append(ctx)
    return " ".join(parts)


async def run_grounded_qa(ctrl, speaker: str, topic: str, *, raw: str = "",
                          source: str = "ambient_qa",
                          recent_context: str | None = None) -> str:
    """handler 本體（放這裡而非 voice_controller，守 size budget 棘輪）。

    D7：ack 先出（貴呼叫前給「查詢中」提示，4.5s 死等會被讀成崩潰），再走 free→付費
    grounded 鏈。L1/L2 幻覺 guard 擋下就回「查不到」。用到的 ctrl 介面：_play_ack /
    play_tts / active_text_channel / stt_logger / bot.router。

    `source` 標記呼叫來源，寫進 `records/ambient_qa.jsonl`，讓「馬文幫我查X」
    （GroundedQAAgent regex 觸發，預設 "ambient_qa"）跟其他重用這條管線的呼叫點
    （例如 NowPlaying 追問視窗接住的開放式補充問題，"music_followup"）在分析時
    分得開，不影響既有呼叫點（保留預設值）。

    回傳 "not_question" / "no_answer" / "answered"，供 `try_question_opener` 判斷
    這句開場話有沒有被接走。
    """
    import time as _time

    _note = getattr(ctrl, "_wake_session_note_grounded", None)   # 🗣️ WakeSession：本 session 進入查詢模式
    if source != "question_opener" and callable(_note):
        _note(speaker)

    _addr = getattr(ctrl, "_reply_address_prefix", None)

    def _with_addr(text: str) -> str:
        p = _addr(speaker, text) if callable(_addr) else ""
        if not isinstance(p, str):   # MagicMock 回傳不算
            p = ""
        return p + text

    asyncio.create_task(ctrl._play_ack("status", speaker=speaker))

    router = getattr(ctrl.bot, "router", None)
    guard = getattr(ctrl, "_ambient_qa_guard", None)
    if guard is None:
        from llm_paid import PaidUsageGuard
        guard = PaidUsageGuard(daily_cap_usd=2.0, monthly_cap_usd=10.0)
        ctrl._ambient_qa_guard = guard

    if recent_context is None:
        recent_context = await build_recent_transcript_context(ctrl, minutes=10)
    games = current_games(ctrl, speaker)

    # 10/23 遊戲名誤聽對照表（docs/designs/game-alias-table.md）：原句命中使用者確認過的誤聽說法才加強提示
    alias_matches = game_aliases.match_games(topic, game_aliases.load_aliases())
    hint = game_aliases.alias_hint(alias_matches)
    alias_names = [e["name"] for _, e in alias_matches]

    _hist_fn = getattr(ctrl, "_wake_session_qa_history", None)
    session_history = _hist_fn(speaker) if callable(_hist_fn) else []
    if not isinstance(session_history, list):   # MagicMock 回傳不算
        session_history = []

    extra_kwargs: dict = {}
    if source == "session_followup":
        extra_kwargs["system_prompt"] = _SESSION_SYSTEM_PROMPT
    elif source == "question_opener":
        extra_kwargs["system_prompt"] = _OPENER_SYSTEM_PROMPT
    extra_kwargs["model_chain"] = QA_MODEL_CHAIN
    extra_kwargs["timeout"] = QA_TIMEOUT_S

    # 只整理第一句（10/23 使用者定）：已有前幾輪問答＝追問，存進對談紀錄的第一句已是整理後的問題，
    # 原句＋上下文給 lite 即可，省約 3.4s。追問被帶偏的風險上線後用重問率量。
    rewritten = None if session_history else await rewrite_question(
        getattr(router, "google_paid_client", None), guard, topic,
        speaker=speaker, recent_context=recent_context,
        session_history=session_history, games=games, alias_hint=hint,
    )
    if rewritten == "" and source in ("question_opener", "session_followup"):
        ctrl.stt_logger.info(f"[BOT→{speaker}] (AmbientQA 整理判定非查詢→交回聊天) {topic}")
        record_ambient_qa({"ts": _time.time(), "speaker": speaker, "raw": raw,
                           "query": topic, "answer": None, "reason": "not_question",
                           "latency_ms": 0, "source": source, "games": games,
                           "alias": alias_names,
                           "model": None, "rewritten": rewritten})
        return "not_question"

    query_str = rewritten if rewritten else topic
    game_context = None if rewritten else games
    if rewritten:
        rb = readback_text(topic, rewritten)
        if rb is not None:
            asyncio.create_task(ctrl.play_tts(_with_addr(rb), already_in_channel=True))

    meta: dict = {}
    t0 = _time.monotonic()
    res = None
    try:
        res = await grounded_answer(
            None,
            getattr(router, "google_paid_client", None),
            guard, query_str,
            recent_context=recent_context,
            game_context=game_context,
            session_history=session_history,
            alias_hint=hint,
            meta=meta,
            **extra_kwargs,
        )
    except Exception as e:
        logger.warning(f"[AmbientQA] grounded_answer 例外: {e}")
    latency_ms = int((_time.monotonic() - t0) * 1000)
    n_context = len([l for l in (recent_context or "").splitlines() if l.strip()])
    prov = format_provenance(meta, latency_ms, len(session_history), n_context, games)

    if res is NOT_A_QUESTION:
        ctrl.stt_logger.info(f"[BOT→{speaker}] (AmbientQA 非查詢→交回聊天 | {prov}) {topic}")
        record_ambient_qa({"ts": _time.time(), "speaker": speaker, "raw": raw,
                           "query": topic, "answer": None, "reason": "not_question",
                           "latency_ms": latency_ms, "source": source, "games": games,
                           "alias": alias_names,
                           "model": meta.get("model"), "rewritten": rewritten})
        return "not_question"

    if res is None:
        ctrl.stt_logger.info(f"[BOT→{speaker}] (AmbientQA 查不到 | {prov}) {topic}")
        asyncio.create_task(ctrl.play_tts(_with_addr("這題我查不到。"), already_in_channel=True))
        record_ambient_qa({"ts": _time.time(), "speaker": speaker, "raw": raw,
                           "query": topic, "answer": None, "reason": "no_answer",
                           "latency_ms": latency_ms, "source": source, "games": games,
                           "alias": alias_names,
                           "model": meta.get("model"), "rewritten": rewritten})
        # 任何 source 都回報 WakeSession（regex 路徑查不到也要計數，否則 session 停不下來，10/17 事故）
        _report = getattr(ctrl, "_wake_session_on_followup_result", None)
        if callable(_report):
            _report(speaker, False)
        if source == "question_opener" and callable(_note):
            _note(speaker)
        return "no_answer"

    answer, sources = res
    ctrl.stt_logger.info(f"[BOT→{speaker}] (AmbientQA | {prov}) {answer}")
    asyncio.create_task(ctrl.play_tts(_with_addr(answer), already_in_channel=True))
    if ctrl.active_text_channel:
        src = f"\n_來源：{', '.join(sources)}_" if sources else ""
        asyncio.create_task(ctrl.active_text_channel.send(
            f"🔎 **【查詢】** `{speaker}`：{answer}{src}"))
    record_ambient_qa({"ts": _time.time(), "speaker": speaker, "raw": raw,
                       "query": topic, "answer": answer, "sources": sources,
                       "latency_ms": latency_ms, "downstream": "none", "source": source,
                       "games": games, "alias": alias_names,
                       "model": meta.get("model"), "rewritten": rewritten})
    _report = getattr(ctrl, "_wake_session_on_followup_result", None)
    if callable(_report):
        _report(speaker, True)
    _note_qa = getattr(ctrl, "_wake_session_note_qa", None)
    if callable(_note_qa):
        _note_qa(speaker, query_str, answer)
    if source == "question_opener" and callable(_note):
        _note(speaker)
    logger.info(f"[AmbientQA] {speaker} 「{topic}」已回答（{latency_ms}ms, src={sources}）。")
    return "answered"


async def try_question_opener(ctrl, ctx) -> bool:
    """IntentBus 沒人接的喚醒句 / 對談窗追問：像問句 → 3.5-flash 自己判斷要不要查。

    True＝已處理（有答案或「這題我查不到」）；False＝不是查詢，交回 Marvin 主聊天。
    10/21 使用者定案：開場說法每個人不一樣 → 句型 regex 補不完，問句形狀就交給模型判斷；
    模型回「略」一律交回聊天（對談窗內也是——安靜會被當成沒在聽）。
    session 已進查詢模式（sticky）→ source=session_followup（連續對談 prompt），否則 question_opener。
    """
    pipeline_timing.mark_once("opener_start")
    try:
        if getattr(ctx, "low_confidence_wake", False) is True:
            return False
        if getattr(ctx, "mode", "normal") not in GroundedQAAgent.mode_compatible:
            return False
        q = _WAKE_PREFIX_RE.sub("", (getattr(ctx, "query", "") or "").strip()).strip()
        if _excluded(q) is not None or _SESSION_PLAY_RE.search(q):
            return False
        if len(_HAN_RE.findall(q)) < 2 or not _SESSION_QUESTION_RE.search(q):
            return False
        sticky_fn = getattr(ctrl, "_wake_session_grounded_sticky", None)
        sticky = callable(sticky_fn) and sticky_fn(ctx.speaker) is True   # is True：MagicMock 回傳不算
        source = "session_followup" if sticky else "question_opener"
        status = await run_grounded_qa(ctrl, ctx.speaker, q, raw=getattr(ctx, "raw_text", "") or "",
                                       source=source)
        handled = status != "not_question"
        if handled:
            turn_outcome.mark_route("question_opener")
        return handled
    finally:
        pipeline_timing.mark_once("opener_done")


def maybe_dispatch_followup_supplement(
    ctrl, speaker: str, pending: dict | None, raw_text: str, now: float, window_s: float,
    has_signal_fn,
) -> bool:
    """NowPlaying 追問視窗接到非肯定詞但有實質內容的回話（「這是哪一年的」之類）
    → fire-and-forget 轉去 grounded QA 回答，回 True 讓 caller `elif ...: return`。

    放這裡（而非 voice_controller）守 size budget 棘輪——呼叫端只需一行 elif。
    """
    from wake_followup import wants_supplement, build_supplement_topic
    if not wants_supplement(pending, raw_text, now, window_s, has_signal_fn):
        return False
    ctrl._pending_followups.pop(speaker, None)
    ctrl.stt_logger.info(f"[💬Followup→補充] [{speaker}] raw='{raw_text[:30]}'")
    asyncio.create_task(run_grounded_qa(
        ctrl, speaker, build_supplement_topic(pending, raw_text),
        raw=raw_text, source="music_followup",
    ))
    return True


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
            # (?P<topic>…) named group 讓 regex 路徑自己填 topic slot；
            # 真正的觸發/排除判斷在 post_match_filter（parse_grounded_qa）。
            # required_slots=["topic"] 同時讓 audio-rescue manifest 把 topic 曝成
            # Gemini function 參數——糊字喚醒問句 → LLM 聽音訊把乾淨問題填進 topic。
            self._cache = [
                IntentSchema(
                    "factual_question", 0.75,
                    patterns=[
                        _LOOKUP_RE.pattern,
                        _FACTUAL_TAIL_RE.pattern,
                    ],
                    required_slots=["topic"],
                    reason_template="ambient_qa",
                    manifest_description=(
                        "使用者在問一個需要查證的事實、生活、常識或即時資訊問題（人事時地物、數字、"
                        "定義、怎麼做、評價、新聞、天氣、某某是什麼/是誰/好不好）。把他要問的東西整理成通順的查詢"
                        "字串放進 topic。不是問正在播的歌、不是問對話歷史、不是點歌時用。"
                    ),
                ),
            ]
        return self._cache

    def gate(self, ctx: IntentContext) -> str | None:
        # audio-rescue：LLM 已聽過音訊才選中這個 intent，比 wake 信心啟發式強，不擋。
        if is_audio_rescue(ctx):
            return None
        if getattr(ctx.rescue, "low_confidence_wake", False):
            return "low_confidence_wake"
        return None

    def post_match_filter(self, schema, slots, ctx: IntentContext) -> bool:
        # audio-rescue：topic 由 LLM 從音訊填好，ctx.query 是糊掉的 STT，別再 re-parse。
        if is_audio_rescue(ctx):
            return audio_rescue_slots_present(slots, "topic")
        parsed = parse_grounded_qa(ctx.query or "")
        if parsed is not None:
            slots["topic"] = parsed
            return True
        return False

    def make_handler(self, schema, slots, ctx: IntentContext):
        if is_audio_rescue(ctx):
            topic = audio_rescue_slot(slots, "topic", ctx)
        else:
            # regex 路徑：parse_grounded_qa 會剝喚醒詞前綴，比 raw named group 乾淨
            topic = parse_grounded_qa(ctx.query or "") or (ctx.query or "")

        async def _answer():
            await run_grounded_qa(self.ctrl, ctx.speaker, topic, raw=ctx.query or "")

        return _answer
