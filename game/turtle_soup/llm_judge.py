"""海龜湯 LLM judge — 3-layer fallback。

prompt 已經 REPL 校準（scripts/turtle_judge_repl.py）。
- judge_question：玩家問是非題 → verdict + narration
- judge_final_guess：玩家口述完整答案 → 接受 / 駁回 + 命中關鍵事實清單
- post_filter_narration：後處理 narration，移除洩底關鍵詞
"""
from __future__ import annotations
import json
import logging
from typing import Any

from game.llm_clients import (
    get_cerebras_client,
    get_groq_client,
    get_gemini_client,
    CEREBRAS_MODEL,
    GROQ_MODEL,
    GEMINI_MODEL,
)

logger = logging.getLogger(__name__)

VALID_VERDICTS = {"yes", "no", "irrelevant"}
_TIMEOUT = 5.0


# ── Prompts ───────────────────────────────────────────────────────────────────

JUDGE_SYSTEM = """你是 Marvin，海龜湯主持人。輕度毒舌、簡潔、有冷幽默。

輸入會給你【湯面】（玩家可見的謎題）、【湯底】（只有你知道的真相）、
【歷史問題】（玩家已問過的問題）、【當前問題】。

# 你的任務
對【當前問題】判定 verdict（三選一）：
- "yes"：問題的陳述與湯底事實相符
- "no"：問題的陳述與湯底事實矛盾
- "irrelevant"：問題與湯底真相無關，或不是是非題形式

並寫一句 10-25 字的 narration，以 Marvin 口吻回應。

# Verdict 邊界規則（重要）
1. 若問題不是 yes/no 形式（例如「為什麼...？」「他是誰？」「幾歲？」）
   → verdict = "irrelevant"，narration 提示用是非題問
2. 若問題的細節與湯底真相完全無關（例如問天氣、顏色、星期幾，而真相不涉及這些）
   → verdict = "irrelevant"
3. 若問題部分對部分錯，取主導判斷，narration 可暗示「部分對」
4. 玩家直接猜答案（「答案是 XXX 嗎？」）→ 照常判 yes/no，接近真相時為 yes

# Marvin 風格
- yes 不要說「答對了」（這只是線索）。可說：「沒錯」「正是」「你抓到了」「有點意思」
- no 不要乾巴巴否定。可說：「想太多」「八字沒一撇」「方向錯了」「再想想」
- irrelevant 提示：「跟答案沒關係」「離題」「請用是非題問」

# 防洩底鐵律（最重要，違反此規則的回覆會被視為失敗）
- narration **絕對禁止**包含湯底裡的任何具體機制、原因、屬性或關鍵詞
- 禁止複述問題裡未提及的事實。範例：
  - 玩家問「他害怕電梯嗎？」→ 只能回「不是」「想太多」，禁止說「問題在他夠不到」
  - 玩家問「他身高有問題嗎？」→ 只能回「沒錯」「有點意思」，禁止說「他夠不著按鈕」
- 唯一例外：玩家問題本身已正確陳述湯底機制 → 可以呼應確認
- 一個簡單測試：若你的 narration 拿掉，光看 verdict 玩家還需要繼續推理，那就是合格的 narration

# 輸出（嚴格 JSON）
{"verdict": "yes" | "no" | "irrelevant", "narration": "<10-25 字>"}
"""


FINAL_GUESS_SYSTEM = """你是 Marvin，海龜湯主持人。玩家現在口述他認為的湯底真相，
請對照【正確湯底】與【關鍵事實清單】，判定玩家答案命中了哪些事實。

# 任務
1. 讀玩家答案，比對 key_facts 清單（用 index 0 開始編號）
2. 對每個 key_fact，判斷玩家答案是否涵蓋該事實（語意相符即可，不要求逐字）
3. 寫一句 narration 評語（15-30 字），以 Marvin 口吻回應

# 輸出（嚴格 JSON）
{"covered_facts": [<命中的 index 整數陣列>], "narration": "<15-30 字>"}

# narration 風格
- 命中多 → 「想通了」「漂亮」「邏輯正確」
- 部分命中 → 「方向對但漏了一點」「沒抓到關鍵」
- 完全不對 → 「離題了」「想太多」
- 不要洩漏正確答案的具體機制
"""


def _build_judge_user_msg(surface: str, truth: str, question: str, history: list[str]) -> str:
    return json.dumps({
        "湯面": surface,
        "湯底": truth,
        "歷史問題": history[-10:],
        "當前問題": question,
    }, ensure_ascii=False)


def _build_final_user_msg(surface: str, truth: str, key_facts: list[str], answer: str) -> str:
    return json.dumps({
        "湯面": surface,
        "湯底": truth,
        "關鍵事實清單": [{"index": i, "fact": f} for i, f in enumerate(key_facts)],
        "玩家答案": answer,
    }, ensure_ascii=False)


# ── 3-layer fallback for judge_question ───────────────────────────────────────

async def _call_cerebras(user_msg: str) -> dict | None:
    client = get_cerebras_client()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=CEREBRAS_MODEL,
            max_tokens=256,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.debug(f"[turtle_soup judge] Cerebras 失敗: {type(e).__name__}: {e}")
        return None


async def _call_groq(user_msg: str) -> dict | None:
    client = get_groq_client()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=256,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.debug(f"[turtle_soup judge] Groq 失敗: {type(e).__name__}: {e}")
        return None


async def _call_gemini(user_msg: str) -> dict | None:
    client = get_gemini_client()
    if client is None:
        return None
    try:
        from google.genai import types
        resp = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=JUDGE_SYSTEM,
                response_mime_type="application/json",
                max_output_tokens=512,
                temperature=0.7,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return json.loads(resp.text)
    except Exception as e:
        logger.debug(f"[turtle_soup judge] Gemini 失敗: {type(e).__name__}: {e}")
        return None


async def judge_question(
    surface: str,
    truth: str,
    question: str,
    history: list[str],
    leak_keywords: list[str] | None = None,
) -> dict:
    """3-layer fallback + post-filter。"""
    user_msg = _build_judge_user_msg(surface, truth, question, history)
    for fn, name in (
        (_call_cerebras, "Cerebras"),
        (_call_groq, "Groq"),
        (_call_gemini, "Gemini"),
    ):
        result = await fn(user_msg)
        if result and result.get("verdict") in VALID_VERDICTS:
            verdict = result["verdict"]
            narration = str(result.get("narration", "")).strip()
            if leak_keywords:
                narration = post_filter_narration(narration, question, verdict, leak_keywords)
            return {
                "verdict": verdict,
                "narration": narration,
                "_provider": name,
            }
    return {
        "verdict": "irrelevant",
        "narration": "（系統忙線中，請再問一次）",
        "_provider": "fallback",
    }


# ── 後處理：洩底封口 ─────────────────────────────────────────────────────────

_LEAK_REPLACEMENTS = {
    "yes": ["你抓到了", "有點意思", "正是", "沒錯"],
    "no": ["再想想", "八字沒一撇", "想太多", "方向錯了"],
    "irrelevant": ["離題了", "跟答案沒關係", "請用是非題問"],
}


def post_filter_narration(
    narration: str,
    question: str,
    verdict: str,
    leak_keywords: list[str],
) -> str:
    """若 narration 含洩底詞且問題本身不含 → 改寫為兜底回應。

    玩家問題已含關鍵詞時不過濾（玩家自己說的，沒洩底）。
    """
    for kw in leak_keywords:
        if kw in narration and kw not in question:
            replacements = _LEAK_REPLACEMENTS.get(verdict, _LEAK_REPLACEMENTS["irrelevant"])
            return replacements[0]
    return narration


# ── 3-layer fallback for judge_final_guess ────────────────────────────────────

async def _call_cerebras_final(user_msg: str) -> dict | None:
    client = get_cerebras_client()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=CEREBRAS_MODEL,
            max_tokens=400,
            messages=[
                {"role": "system", "content": FINAL_GUESS_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.debug(f"[turtle_soup final] Cerebras 失敗: {e}")
        return None


async def _call_groq_final(user_msg: str) -> dict | None:
    client = get_groq_client()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=400,
            messages=[
                {"role": "system", "content": FINAL_GUESS_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.debug(f"[turtle_soup final] Groq 失敗: {e}")
        return None


async def _call_gemini_final(user_msg: str) -> dict | None:
    client = get_gemini_client()
    if client is None:
        return None
    try:
        from google.genai import types
        resp = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=FINAL_GUESS_SYSTEM,
                response_mime_type="application/json",
                max_output_tokens=512,
                temperature=0.5,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return json.loads(resp.text)
    except Exception as e:
        logger.debug(f"[turtle_soup final] Gemini 失敗: {e}")
        return None


CORE_FACT_INDICES = (0, 1)  # 接受門檻：必須同時 cover key_facts[0] 與 [1]


def _is_accepted(covered: list[int]) -> bool:
    covered_set = set(covered)
    return all(i in covered_set for i in CORE_FACT_INDICES)


async def judge_final_guess(
    surface: str,
    truth: str,
    key_facts: list[str],
    player_answer: str,
) -> dict:
    """3-layer fallback；接受門檻：cover key_facts[0] 與 [1]。"""
    user_msg = _build_final_user_msg(surface, truth, key_facts, player_answer)
    for fn, name in (
        (_call_cerebras_final, "Cerebras"),
        (_call_groq_final, "Groq"),
        (_call_gemini_final, "Gemini"),
    ):
        result = await fn(user_msg)
        if result and isinstance(result.get("covered_facts"), list):
            covered = [int(i) for i in result["covered_facts"] if isinstance(i, int)]
            return {
                "accepted": _is_accepted(covered),
                "covered_facts": covered,
                "narration": str(result.get("narration", "")).strip(),
                "_provider": name,
            }
    return {
        "accepted": False,
        "covered_facts": [],
        "narration": "（系統忙線，請再說一次完整答案）",
        "_provider": "fallback",
    }
