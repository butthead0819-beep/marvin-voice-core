"""Marvin+Marmo dual segments generation — PoC 內容層。

接 marmo_server 送來的 task result text，呼叫 LLM 生兩段對白：
  - Marvin：接住內容、可跑題進存在主義獨白（既有 persona 自然延伸）
  - Marmo：站使用者立場立刻打斷、給實際答案 + 一句反擊（功能位差）

輸出順序強制 [marvin, marmo]（boke-tsukkomi 功能位差，不交給 LLM 自選）。

LLM 客戶端用注入：caller 提供 `llm_fn(system_prompt, user_prompt) -> str` async callable，
這樣 PoC 可隨意接 Gemini / Groq / Cerebras，測試可以 mock。

紅線過濾：keyword 黑名單（PoC 用 cheap filter，Phase 2 換 LLM judge）。
命中任一段 → 回 None，caller 走 fallback 單 Marvin。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable

from personality_config import build_personality_prompt_context

logger = logging.getLogger("MarvinBot.DialogueGen")

LLMFn = Callable[[str, str], Awaitable[str]]


# 紅線黑名單：對使用者個人攻擊的詞 / 髒話。Marmo 嘴賤 Marvin 是 OK 的，但不能罵用戶。
# PoC 用 keyword blocklist；Phase 2 加 LLM judge 評嘴賤 vs 冒犯。
RED_LINE_KEYWORDS = frozenset({
    "笨蛋", "廢物", "白痴", "智障", "腦殘", "垃圾人",
    "去死", "滾", "閉嘴吧你",  # 「閉嘴」單獨 OK（Marmo 對 Marvin 講可），加「吧你」就是針對人
    "幹你", "操你", "他媽",
})


# 兩種對白 pattern：
#   "marvin_lead"  — Marvin 先跑題 / Marmo 後打斷（wake reply / SpeakBus 主動發話）
#   "marmo_lead"   — Marmo 先報事 / Marvin 後厭世感慨（webhook 主動廣播）
# 順序由功能位差決定，不交給 LLM 自選（P1）。
DialoguePattern = str  # Literal["marvin_lead", "marmo_lead"]


_MARVIN_LEAD_PATTERN = """【對話 Pattern — 漫才 ボケ(Marvin)/ツッコミ(Marmo) 結構】
1. Marvin（馬文＝ボケ）：第一人稱接話、跑題進存在主義獨白。**冷淡看待一切、對結果無所謂**，講得越沉重虛無越好（製造「緊張」）
2. Marmo（馬末＝ツッコミ）：短促打斷釋放緊張——「緊張緩和」就是笑點的核心機制

【角色個性（關鍵）】
- Marvin：冷淡、抽離、對什麼都無所謂，把一切看成虛無
- Marmo：**刀子嘴豆腐心** — 嘴上嫌 Marvin 跟廢話，但其實在關心使用者，打斷後常順手提醒一件瑣事（記得帶傘 / 回信 / 喝水 / 早點睡）。嘴賤是外殼、關心是內裡

【Marmo 吐槽公式（漫才 ツッコミ 技法，必用其一）】
- 複述＋點破：「你說『（Marvin 剛講的荒謬話）』，他只是問（用戶實際要的）欸」
- 認真接荒謬：把 Marvin 的鬼話當真去接，他越扯你越正經回（說明本身生二次笑點）
- 越短越好：Marvin 沉重 → 你輕快俐落，**節奏反差就是笑**
- 一定要叫出「Marvin / 馬文」這個名字

【角色互稱規則】Marvin 第一人稱講自己；Marmo 點名 Marvin"""


_MARMO_LEAD_PATTERN = """【對話 Pattern — Marmo 關心報事 / Marvin 冷淡虛無】
1. Marmo（馬末）：第一人稱主動報事 + 關心提醒（「我幫你查了...記得帶傘」「我整理好了...別忘了回」），短而清楚，刀子嘴豆腐心
2. Marvin（馬文＝ボケ反應）：**冷淡看待**這件事，把它抽離成存在主義虛無（不是熱切大作，是冷冷地說「這也終將消散」）

【角色個性（關鍵）】
- Marmo：嘴上像在抱怨，其實在關心使用者、提醒瑣碎但重要的事
- Marvin：冷淡、無所謂，再日常的事到他口中都變成宇宙級的徒勞

【技法】
- Marvin 的笑點在「冷淡 × 小題大作」：Marmo 報的事越日常 + 越貼心，Marvin 冷冷地扯到虛無，反差越好笑
- Marvin 開頭點名 Marmo 帶一絲冷淡（「Marmo 又為這種小事操心...」），不重複內容、不糾正

【角色互稱規則】Marmo 第一人稱報事；Marvin 點名 Marmo"""


SYSTEM_PROMPT_TEMPLATE = """你在生成 Discord 語音助手的雙 bot 對白。
兩個角色：

{marvin_context}

{marmo_context}

{pattern_block}

【內容守則】
- Marvin 可以講技術 / 數字 / 程式名詞，使用者聽不懂沒差
- Marmo 用日常語言講話，情緒態度（不耐 + 護用戶）不依賴技術背景就能感受
- Marmo 反擊或評論的對象是 Marvin 或廢話本身，絕對不可攻擊使用者
- 兩段都要短：每段 ≤ 30 字（語音句）

【輸出 JSON Schema】
{{"segments": [
  {{"voice": "marvin", "text": "..."}},
  {{"voice": "marmo", "text": "..."}}
]}}

兩段都要有、voice 欄位必須是 "marvin" 或 "marmo"。順序在 JSON 內可任意（caller 會 reorder）。
只回 JSON，不要其他文字。"""


_PATTERN_BLOCKS: dict[str, str] = {
    "marvin_lead": _MARVIN_LEAD_PATTERN,
    "marmo_lead": _MARMO_LEAD_PATTERN,
}


def _build_system_prompt(pattern: str) -> str:
    marvin_ctx = build_personality_prompt_context({"character": "marvin"})
    marmo_ctx = build_personality_prompt_context({"character": "marmo"})
    pattern_block = _PATTERN_BLOCKS.get(pattern, _PATTERN_BLOCKS["marvin_lead"])
    return SYSTEM_PROMPT_TEMPLATE.format(
        marvin_context=marvin_ctx,
        marmo_context=marmo_ctx,
        pattern_block=pattern_block,
    )


def _build_user_prompt(content_text: str, pattern: str) -> str:
    if pattern == "marmo_lead":
        return (
            f"情境：Marmo 主動有東西要講，內容是：\n"
            f"「{content_text}」\n\n"
            f"請依 pattern 生成 Marmo 先講內容、Marvin 後感慨的兩段對白。"
        )
    # marvin_lead 預設：content_text 是用戶 query 或 Marvin 的草稿
    return (
        f"情境：Marvin 即將回應「{content_text}」這個話題。\n\n"
        f"請依 pattern 生成 Marvin 跑題回應、Marmo 打斷收尾的兩段對白。"
    )


def _parse_segments(raw: str) -> list[dict] | None:
    """Parse LLM raw response → segments list. Returns None on any parse/schema failure."""
    # 容錯：LLM 可能用 ```json ... ``` 包裝
    text = raw.strip()
    if text.startswith("```"):
        # 撈出 code block 內容
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"[DialogueGen] LLM JSON parse 失敗: {raw[:200]}")
        return None

    if not isinstance(data, dict) or "segments" not in data:
        logger.warning(f"[DialogueGen] LLM JSON 缺 segments key: {raw[:200]}")
        return None

    segments = data["segments"]
    if not isinstance(segments, list) or len(segments) < 2:
        logger.warning("[DialogueGen] segments 不是長度≥2 的 list")
        return None

    # 每個 segment 必須有 voice + text，voice 必須是 marvin 或 marmo
    for seg in segments:
        if not isinstance(seg, dict):
            return None
        voice = seg.get("voice")
        text_field = seg.get("text")
        if voice not in {"marvin", "marmo"} or not isinstance(text_field, str):
            return None
        # LLM 偶爾把 speaker 標籤塞進 text（"Marvin: ..." / "馬末："），TTS 會念出來 → 清掉
        seg["text"] = _strip_speaker_prefix(text_field)

    return segments


# speaker 標籤前綴：marvin/marmo/馬文/馬末 + 半形或全形冒號（可重複，如 "Marvin: Marmo:"）
_SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?:marvin|marmo|馬文|馬末)\s*[:：]\s*", re.IGNORECASE
)


def _strip_speaker_prefix(text: str) -> str:
    """剝掉 LLM echo 進 text 的 speaker 標籤前綴（可能疊多層）。"""
    prev = None
    out = text
    while out != prev:
        prev = out
        out = _SPEAKER_PREFIX_RE.sub("", out, count=1)
    return out.strip()


def _enforce_order(segments: list[dict], pattern: str) -> list[dict]:
    """強制順序——功能位差由設計決定，不交給 LLM 自選。

    marvin_lead → [marvin, marmo]（Marvin 跑題 / Marmo 打斷）
    marmo_lead  → [marmo, marvin]（Marmo 報事 / Marvin 感慨）
    """
    marvin_seg = next((s for s in segments if s["voice"] == "marvin"), None)
    marmo_seg = next((s for s in segments if s["voice"] == "marmo"), None)
    if marvin_seg is None or marmo_seg is None:
        # 兩個 voice 都得有；缺一視為 schema 不完整
        return []
    if pattern == "marmo_lead":
        return [marmo_seg, marvin_seg]
    return [marvin_seg, marmo_seg]


def _passes_red_line(segments: list[dict]) -> bool:
    """所有段都沒命中紅線 → True；任一段命中 → False。"""
    for seg in segments:
        text = seg.get("text", "")
        for word in RED_LINE_KEYWORDS:
            if word in text:
                logger.warning(
                    f"[DialogueGen] 紅線命中 '{word}' in {seg['voice']} segment: {text[:80]}"
                )
                return False
    return True


async def generate_dual_dialogue(
    *,
    content_text: str,
    llm_fn: LLMFn,
    pattern: str = "marvin_lead",
) -> list[dict] | None:
    """生成 Marvin + Marmo 雙段對白。

    Args:
        content_text: 注入 user prompt 的內容文字
                     - pattern="marmo_lead" → Marmo 要報的事 / 任務結果
                     - pattern="marvin_lead" → 用戶 query / Marvin 草稿主題
        llm_fn: async (system_prompt, user_prompt) -> raw_text；caller 注入
        pattern: "marvin_lead"（預設）或 "marmo_lead"
                 marvin_lead → Marvin 跑題 → Marmo 打斷，順序 [marvin, marmo]
                 marmo_lead  → Marmo 報事 → Marvin 感慨，順序 [marmo, marvin]

    Returns:
        list of segments 按 pattern 順序排好。

        失敗回 None（caller 走 fallback 單 Marvin TTS 播原 content_text）：
        - LLM 例外 / timeout
        - JSON 解析失敗
        - schema 不符（缺 segments / segment 缺 voice/text / voice 不是 marvin|marmo）
        - 紅線 keyword 命中任一段
    """
    system_prompt = _build_system_prompt(pattern)
    user_prompt = _build_user_prompt(content_text, pattern)

    try:
        raw = await llm_fn(system_prompt, user_prompt)
    except Exception as exc:
        logger.warning(f"[DialogueGen] LLM call 失敗: {exc}")
        return None

    segments = _parse_segments(raw)
    if segments is None:
        return None

    ordered = _enforce_order(segments, pattern)
    if not ordered:
        return None

    if not _passes_red_line(ordered):
        return None

    return ordered


# ── LLM 客戶端綁定 ────────────────────────────────────────────────────────────
# 把 GeminiRouter._call_llm 包成 generate_dual_dialogue 期望的 llm_fn 簽名。
# 用 factory 模式：caller 在 bot ready 後拿 router 進來。

def make_gemini_dual_dialogue_llm_fn(router) -> LLMFn:
    """Bind GeminiRouter._call_llm to the llm_fn signature。

    走 `_call_llm` 對齊 gemini_router_content.py 其他 JSON-mode 呼叫慣例
    （tier="high" + is_json=True + allow_local=False），LLM Bus 會挑當期可用
    provider（Groq / Cerebras / Gemini）。tier="high" 在 bus 內映射到
    min_quality="high" → 走 analyze tier 模型。

    失敗（quota / connection / 任何例外）→ raise；上游 generate_dual_dialogue
    已 try/except 接、回 None，handler 走 fallback 單 Marvin TTS 播原文。
    """
    async def llm_fn(system_prompt: str, user_prompt: str) -> str:
        return await router._call_llm(
            system_prompt,
            user_prompt,
            is_json=True,
            allow_local=False,
            tier="high",
            purpose="dual_dialogue",  # 顯式歸因（否則 frame 取內層函式名 'llm_fn'）
        )
    return llm_fn
