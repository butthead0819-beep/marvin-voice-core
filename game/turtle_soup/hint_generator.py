"""海龜湯 hint 編織網 generator — top-down 抽節點 + bottom-up 組提示。

設計概念（v0.5）：hint 不是線性 1D/2D/3D，而是「節點 + 揭露關係」網。
  HintNode：atomic insight（推理鏈中的一環）
  Hint：提示文字 + 它揭露哪些節點

LLM 流程：
  Top-down（先抽節點）：從湯底逆推，找出 N 個推理節點（A → B → C），
                       每個節點是一個 atomic insight。
  Bottom-up（後組提示）：用節點當積木，組出 K 條 hint，每條揭露不同節點子集。
                       後面的 hint 必須包含前面的節點（單調遞進不撤回）。

兩階段在一個 LLM call 內完成（一次 JSON 輸出兩段：nodes + hints）。

3-layer fallback：Cerebras → Groq → Gemini（與 judge 共用 client）。
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


SYSTEM = """你是海龜湯題目的提示設計師。給定一道題目的湯面、湯底、key_facts、leak_keywords，
你要先「抽推理節點」、再「組提示網點」。

# 第一階段：top-down 抽 hint_nodes（推理節點）
從湯底逆推，找出 3-5 個 atomic insight 節點（A → B → C → ...）。
每個節點：
- 是一個獨立的、無法再拆的洞察（例如「主角身體有不尋常的限制」是一個節點）
- 節點之間有推理依賴：後面的節點通常需要前面的節點才能理解
- 給每個節點一個簡短英文 id（snake_case）和中文 fact 描述
- **加 keywords**：3-8 個玩家可能在問題中用到的中文詞（個人化排序用）
  - 例：身體限制節點 keywords = ["身高", "身材", "身體", "矮", "個子"]
  - 例：依賴他人節點 keywords = ["別人", "朋友", "陪同", "幫忙", "獨自"]

# 第二階段：bottom-up 組 hints（提示網點）
用上面的節點當積木，組出 3 條 hint。每條 hint：
- 揭露 1, 2, 3 個節點（依序遞進）
- 後面的 hint 揭露的節點集合，**必須包含**前面 hint 的節點（單調遞進不撤回）
- 提示文字 15-35 字、自然口語、Marvin 主持人語氣
- 不可直接寫出湯底名詞，不可包含 leak_keywords 任何詞
- 揭露的是「方向」，不是「答案本身」

# 鐵律
1. 不可包含 leak_keywords 列表中任何詞
2. 不可直接寫出湯底中的具體名詞
3. reveals 必須是 hint_nodes 中已定義的 id（不可生造）
4. 後 hint.reveals ⊇ 前 hint.reveals（superset 關係）
5. 每條 hint 至少揭露一個前一條沒揭露的節點（嚴格遞進）

# 輸出（嚴格 JSON）
{
  "hint_nodes": [
    {"id": "body_limit", "fact": "主角身體有不尋常的限制", "keywords": ["身高", "身材", "矮", "個子"]},
    {"id": "tool_reach", "fact": "某些工具或設備在他能力範圍外", "keywords": ["按鈕", "按鍵", "夠到"]},
    {"id": "assist_dep", "fact": "獨自時辦不到、有人在場時可以", "keywords": ["別人", "朋友", "獨自", "幫忙"]}
  ],
  "hints": [
    {"text": "想想他身體上的限制會怎麼影響日常動作", "reveals": ["body_limit"]},
    {"text": "為什麼某些操作有人在時可以、自己不行？", "reveals": ["body_limit", "tool_reach"]},
    {"text": "有別人一起時能做到，獨自卻不行 — 這依賴什麼條件？", "reveals": ["body_limit", "tool_reach", "assist_dep"]}
  ]
}
"""


def _build_user_msg(surface: str, truth: str, key_facts: list[str], leak_keywords: list[str]) -> str:
    return json.dumps({
        "湯面": surface,
        "湯底": truth,
        "key_facts": key_facts,
        "leak_keywords": leak_keywords,
    }, ensure_ascii=False)


_TIMEOUT = 10.0


def _validate(raw: Any) -> dict | None:
    """檢查 LLM 輸出符合 graph schema。

    要求：
    - hint_nodes: list[{id, fact}] 至少 2 個
    - hints: list[{text, reveals}] 至少 2 條
    - 每個 hint.reveals 引用的 id 都在 hint_nodes 裡
    - hints 後一條 reveals ⊇ 前一條（單調遞進）
    """
    if not isinstance(raw, dict):
        return None

    nodes_raw = raw.get("hint_nodes")
    hints_raw = raw.get("hints")
    if not isinstance(nodes_raw, list) or len(nodes_raw) < 2:
        return None
    if not isinstance(hints_raw, list) or len(hints_raw) < 2:
        return None

    node_ids: set[str] = set()
    nodes_clean = []
    for n in nodes_raw:
        if not isinstance(n, dict):
            return None
        nid = n.get("id")
        fact = n.get("fact")
        if not isinstance(nid, str) or not nid.strip():
            return None
        if not isinstance(fact, str) or not fact.strip():
            return None
        if nid in node_ids:
            return None  # 重複 id
        # keywords 可選；若有，必須是 str list
        kws_raw = n.get("keywords", [])
        if not isinstance(kws_raw, list):
            return None
        keywords = []
        for kw in kws_raw:
            if not isinstance(kw, str) or not kw.strip():
                continue
            keywords.append(kw.strip())
        node_ids.add(nid)
        clean: dict = {"id": nid.strip(), "fact": fact.strip()}
        if keywords:
            clean["keywords"] = keywords
        nodes_clean.append(clean)

    prev_reveals: set[str] = set()
    hints_clean = []
    for h in hints_raw:
        if not isinstance(h, dict):
            return None
        text = h.get("text")
        reveals = h.get("reveals")
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(reveals, list) or not reveals:
            return None
        for rid in reveals:
            if not isinstance(rid, str) or rid not in node_ids:
                return None
        current = set(reveals)
        if not prev_reveals.issubset(current):
            return None  # 撤回前面節點
        if current == prev_reveals:
            return None  # 沒揭露新內容
        prev_reveals = current
        hints_clean.append({"text": text.strip(), "reveals": list(reveals)})

    return {"hint_nodes": nodes_clean, "hints": hints_clean}


async def _call_cerebras(user_msg: str) -> dict | None:
    client = get_cerebras_client()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=CEREBRAS_MODEL,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT,
        )
        return _validate(json.loads(resp.choices[0].message.content))
    except Exception as e:
        logger.debug(f"[hint_gen] Cerebras 失敗: {type(e).__name__}: {e}")
        return None


async def _call_groq(user_msg: str) -> dict | None:
    client = get_groq_client()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT,
        )
        return _validate(json.loads(resp.choices[0].message.content))
    except Exception as e:
        logger.debug(f"[hint_gen] Groq 失敗: {type(e).__name__}: {e}")
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
                system_instruction=SYSTEM,
                response_mime_type="application/json",
                max_output_tokens=1500,
                temperature=0.7,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return _validate(json.loads(resp.text))
    except Exception as e:
        logger.debug(f"[hint_gen] Gemini 失敗: {type(e).__name__}: {e}")
        return None


def _filter_leaks(graph: dict, leak_keywords: list[str]) -> dict:
    """若任何 hint text 含 leak_keywords → 加 ⚠[LEAK:KW] 標記。

    不直接改寫（離線工具讓作者親自決定保留 / 重生 / 改寫）。
    hint_nodes.fact 不過濾（它是內部欄位，玩家看不到）。
    """
    result = {"hint_nodes": list(graph["hint_nodes"]), "hints": []}
    for h in graph["hints"]:
        text = h["text"]
        for kw in leak_keywords:
            if kw and kw in text:
                text = f"⚠[LEAK:{kw}] {text}"
                break
        result["hints"].append({"text": text, "reveals": h["reveals"]})
    return result


async def generate_hint_graph(
    surface: str,
    truth: str,
    key_facts: list[str],
    leak_keywords: list[str],
) -> dict:
    """3-layer fallback 產生 hint 網（top-down nodes + bottom-up hints）。

    回傳 {
      "hint_nodes": [{"id": str, "fact": str}, ...],
      "hints": [{"text": str, "reveals": [str, ...]}, ...],
      "_provider": "Cerebras" | "Groq" | "Gemini" | "fallback",
    }

    fallback 時兩個 list 都空，呼叫方應人工填寫。
    """
    user_msg = _build_user_msg(surface, truth, key_facts, leak_keywords)
    for fn, name in (
        (_call_cerebras, "Cerebras"),
        (_call_groq, "Groq"),
        (_call_gemini, "Gemini"),
    ):
        result = await fn(user_msg)
        if result:
            filtered = _filter_leaks(result, leak_keywords)
            return {**filtered, "_provider": name}
    return {
        "hint_nodes": [],
        "hints": [],
        "_provider": "fallback",
    }
