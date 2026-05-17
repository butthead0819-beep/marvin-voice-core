#!/usr/bin/env python3
"""海龜湯 LLM Judge prompt 校準 REPL。

獨立於 Discord bot 跑，純驗證 judge prompt 的品質。
不接 STT、不接 TTS、不接 Discord。

用法：
    python scripts/turtle_judge_repl.py

輸入是非題，看 Marvin 怎麼回。在這個 loop 裡反覆校準 prompt 直到語感正確。

需要 env：CEREBRAS_API_KEY / GROQ_API_KEY / GEMINI_API_KEY 至少一個。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from game.llm_clients import (  # noqa: E402
    get_cerebras_client,
    get_groq_client,
    get_gemini_client,
    CEREBRAS_MODEL,
    GROQ_MODEL,
    GEMINI_MODEL,
)


# ─── 種子題目（hardcode v0）───────────────────────────────────────────────────

PUZZLE = {
    "id": "elevator_18f",
    "surface": (
        "男子住在大廈 22 樓。每天他出門上班時搭電梯直達 1 樓。"
        "下班回家時，他只搭電梯到 18 樓，然後走樓梯走完最後 4 層回到 22 樓。"
        "他沒有運動需求，電梯也沒壞。為什麼他要這樣？"
    ),
    "truth": (
        "男子是侏儒，身高只夠按到電梯按鈕的 18 樓位置。"
        "早上下樓沒問題，因為他能按到最低的 1 樓。"
        "晚上回家若電梯裡剛好有別人，他可以拜託對方幫他按 22 樓直達；"
        "但他獨自搭電梯時，只能按到他構得到的最高樓層 18 樓，"
        "剩下 4 層只好走樓梯。"
    ),
    "key_facts": [
        "男子是侏儒（或身材矮小）",
        "電梯按鈕的高度問題",
        "他構不到 22 樓按鈕",
        "18 樓是他能按到的最高樓層",
        "有人陪同搭電梯時可以直達 22 樓",
    ],
}


# ─── Judge prompt ─────────────────────────────────────────────────────────────

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
- 唯一例外：玩家問題本身已正確陳述湯底機制（例如「答案是他按不到按鈕嗎？」）→ 可以呼應確認
- 一個簡單測試：若你的 narration 拿掉，光看 verdict 玩家還需要繼續推理，那就是合格的 narration
- 若 narration 裡包含問題沒提到的湯底詞彙（侏儒、夠不到、按鈕、身高、構不著、樓層數字 1/18/22 等），請改寫

# 輸出（嚴格 JSON）
{"verdict": "yes" | "no" | "irrelevant", "narration": "<10-25 字>"}
"""


def build_user_msg(question: str, asked_history: list[str]) -> str:
    return json.dumps({
        "湯面": PUZZLE["surface"],
        "湯底": PUZZLE["truth"],
        "歷史問題": asked_history[-10:],
        "當前問題": question,
    }, ensure_ascii=False)


# ─── 3-layer LLM fallback ─────────────────────────────────────────────────────

async def call_cerebras(user_msg: str) -> dict | None:
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
            timeout=5.0,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"  [Cerebras 失敗: {type(e).__name__}: {e}]")
        return None


async def call_groq(user_msg: str) -> dict | None:
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
            timeout=5.0,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"  [Groq 失敗: {type(e).__name__}: {e}]")
        return None


async def call_gemini(user_msg: str) -> dict | None:
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
        print(f"  [Gemini 失敗: {type(e).__name__}: {e}]")
        return None


VALID_VERDICTS = {"yes", "no", "irrelevant"}


async def judge(question: str, asked_history: list[str]) -> dict:
    user_msg = build_user_msg(question, asked_history)
    for fn, name in (
        (call_cerebras, "Cerebras"),
        (call_groq, "Groq"),
        (call_gemini, "Gemini"),
    ):
        result = await fn(user_msg)
        if result and result.get("verdict") in VALID_VERDICTS:
            return {**result, "_provider": name}
    return {
        "verdict": "irrelevant",
        "narration": "（系統故障，請再問一次）",
        "_provider": "fallback",
    }


# ─── REPL ─────────────────────────────────────────────────────────────────────

def check_env() -> list[str]:
    """回傳可用 provider 名稱清單。"""
    available = []
    if os.environ.get("CEREBRAS_API_KEY"):
        available.append("Cerebras")
    if os.environ.get("GROQ_API_KEY"):
        available.append("Groq")
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        available.append("Gemini")
    return available


async def main():
    providers = check_env()
    print("=" * 70)
    print(f"海龜湯 Judge REPL  |  可用 provider: {', '.join(providers) or '⚠️ 無'}")
    print("=" * 70)
    if not providers:
        print("\n❌ 找不到任何 LLM API key。設定其中一個環境變數後重跑：")
        print("    export CEREBRAS_API_KEY=...")
        print("    export GROQ_API_KEY=...")
        print("    export GEMINI_API_KEY=...")
        return

    print(f"\n📜 【湯面】\n{PUZZLE['surface']}\n")
    print(f"🔒 【湯底】（測試模式顯示，真實玩家看不到）\n{PUZZLE['truth']}\n")
    print("─" * 70)
    print("輸入是非題試 prompt。指令：'quit' 結束、'show' 重看湯面、'reset' 清空歷史")
    print("─" * 70)

    history: list[str] = []
    while True:
        try:
            q = input("\n🤔 你問: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("quit", "q", "exit"):
            break
        if q.lower() == "show":
            print(f"\n📜 {PUZZLE['surface']}")
            continue
        if q.lower() == "reset":
            history = []
            print("（歷史已清空）")
            continue

        result = await judge(q, history)
        verdict = result["verdict"]
        narration = result["narration"]
        provider = result["_provider"]

        emoji = {"yes": "✅", "no": "❌", "irrelevant": "💨"}.get(verdict, "?")
        print(f"  {emoji} [{verdict:11s}] Marvin: {narration}  ({provider})")
        history.append(q)

    print(f"\n總計問了 {len(history)} 題。bye。")


if __name__ == "__main__":
    asyncio.run(main())
