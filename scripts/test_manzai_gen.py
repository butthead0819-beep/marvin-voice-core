#!/usr/bin/env python3
"""手動測漫才「生成」：建 router → 跑 generate_dual_dialogue → 印對白（不進語音頻道）。

只測對白內容品質（不發聲），最快迭代。要聽實際播放/打岔請用 curl webhook（見 README 註）。

用法（在 bot 的 venv + 有 GEMINI_API_KEY / GOOGLE_API_KEY 的環境）：
  python scripts/test_manzai_gen.py "大家剛在聊週末出去玩結果一直下雨"
  python scripts/test_manzai_gen.py --pattern marmo_lead "Marmo 查到明天會下雨"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _run() -> int:
    ap = argparse.ArgumentParser(description="測漫才對白生成")
    ap.add_argument("content", nargs="?",
                    default="大家剛剛在聊週末要不要出去玩，結果天氣預報說一直下雨")
    ap.add_argument("--pattern", default="marvin_lead",
                    choices=["marvin_lead", "marmo_lead"])
    args = ap.parse_args()

    os.environ.setdefault("LLM_BUS", "true")  # 走免費 bus，跟 production 一致
    try:
        from dotenv import load_dotenv
        load_dotenv()  # 從專案根 .env 載 API key（跟 main_discord 一致）
    except Exception:
        pass
    from gemini_router import GeminiRouter
    from services.dialogue_generation import (
        generate_dual_dialogue,
        make_gemini_dual_dialogue_llm_fn,
    )

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("❌ 沒有 GEMINI_API_KEY / GOOGLE_API_KEY，無法跑生成")
        return 1

    router = GeminiRouter(api_key)
    try:
        router._init_llm_bus()  # 免費 bus；失敗則 _call_llm 自動落 legacy
    except Exception as e:
        print(f"[warn] bus init 失敗，走 legacy 直連: {e}")

    llm_fn = make_gemini_dual_dialogue_llm_fn(router)
    print(f"\n=== content: {args.content}")
    print(f"=== pattern: {args.pattern}（marvin_lead = Marvin 拋題 → Marmo 打斷）\n")

    segments = await generate_dual_dialogue(
        content_text=args.content, llm_fn=llm_fn, pattern=args.pattern,
    )
    if not segments:
        print("❌ 生成回 None（LLM 例外 / JSON parse 失敗 / schema 不符 / 紅線 keyword 命中）")
        return 2
    for s in segments:
        who = "🤖 Marvin" if s.get("voice") != "marmo" else "🦞 Marmo "
        print(f"{who} | {s.get('text')}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
