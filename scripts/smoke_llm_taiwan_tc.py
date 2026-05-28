"""Smoke test：實際打 LLM 驗證 rescue + augment prompt 真的吐台灣繁中。

Unit test 只驗 prompt 內容含「台灣 / 繁體 / 簡體」三詞，沒驗 8b 量級 LLM
是否真的遵守。這個 script 打真 LLM，掃輸出有沒有「簡體獨有字符」，產 verdict。

跑法：
  python scripts/smoke_llm_taiwan_tc.py

成本：~8 次 LLM call（quick tier），總 token < 5000。
退出碼：0 = 全部通過；1 = 有簡體字混入（prompt 需強化）。

未來改 rescue / augment prompt 後重跑驗證，跟 unit test pin 互補。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 載 .env 給 llm_pool 讀 provider keys（bot 用 main_discord.py 的 load_dotenv()
# 走同樣 path，這個 standalone script 必須自己呼一次）
from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from intent_agents.intent_augmentation import (  # noqa: E402
    SchemaInfo, make_augment_prompt, parse_augment_response,
)
from intent_agents.rescue_classifier import make_rescue_classifier  # noqa: E402


# 簡體獨有字符：不在繁中常用字集裡，若 LLM 輸出含這些 → prompt 失敗。
# 不求窮舉（hard exhaustive），收常見高頻簡體字；漏網的 edge case 靠人工複查。
SIMPLIFIED_CHARS = set(
    "视质网们来这个国时会几当实长边进学业队听体关单简动话语门头"
    "马鸟鱼鸡鸭农药汉师页问题间真带样东两书车专写军轻钱钟银录数电"
)


RESCUE_SAMPLES = [
    "希望下次可以找到好聽的歌",
    "我覺得這首不太對",
    "能不能小聲一點",
    "幫我換一首吧",
    "這個音量太大了啦",
]


AUGMENT_SAMPLES = [
    SchemaInfo("playback_control", "skip_track", 0.85,
               ("(下一首|切歌|換歌|跳過)",), "control:skip"),
    SchemaInfo("volume", "volume_down", 0.9,
               ("(小聲|音量小|調低)",), "volume:down"),
    SchemaInfo("now_playing", "now_playing", 0.9,
               ("這首叫什麼", "誰唱的"), "ask:now_playing"),
]


def find_simplified(text: str) -> list[str]:
    return [c for c in text if c in SIMPLIFIED_CHARS]


async def smoke_rescue(router) -> list[tuple]:
    classify = make_rescue_classifier(router)
    print("\n=== Rescue smoke ===")
    issues: list[tuple] = []
    for q in RESCUE_SAMPLES:
        try:
            result = await classify(q)
        except Exception as exc:
            print(f"  ⚠️  [{q}] → exception: {exc}")
            continue
        if result is None:
            print(f"  ─  [{q}] → None (LLM 拒絕 / 低信心)")
            continue
        rewritten = result.get("rewritten_query", "")
        signal = result.get("pragmatic_signal")
        target = result.get("pragmatic_target")
        conf = result.get("confidence", 0)
        bad = find_simplified(rewritten)
        marker = "❌" if bad else "✅"
        print(f"  {marker} [{q}]")
        print(f"      → rewritten={rewritten!r} signal={signal} target={target} conf={conf}")
        if bad:
            print(f"      ⚠ 簡體字: {bad}")
            issues.append(("rescue", q, rewritten, bad))
    return issues


async def smoke_augment(router) -> list[tuple]:
    print("\n=== Augment smoke ===")
    issues: list[tuple] = []
    for schema in AUGMENT_SAMPLES:
        try:
            raw = await router.quick(
                prompt=make_augment_prompt(schema),
                caller="smoke_taiwan_tc",
                json=True,
                max_tokens=400,
                temperature=0.7,
            )
        except Exception as exc:
            print(f"  ⚠️  [{schema.intent_name}] → router exception: {exc}")
            continue
        result = parse_augment_response(raw)
        if result is None:
            print(f"  ─  [{schema.intent_name}] → parse failed (raw={raw[:80] if raw else None!r})")
            continue
        print(f"\n  📋 {schema.agent_name}::{schema.intent_name}")
        for p in result.paraphrases:
            bad = find_simplified(p)
            marker = "❌" if bad else "✅"
            print(f"    {marker} {p}")
            if bad:
                issues.append(("augment", schema.intent_name, p, bad))
        if result.suggested_regex:
            bad = find_simplified(result.suggested_regex)
            marker = "❌" if bad else "✅"
            print(f"    {marker} suggested_regex: {result.suggested_regex}")
            if bad:
                issues.append(("augment_regex", schema.intent_name, result.suggested_regex, bad))
    return issues


async def main() -> int:
    from llm_pool import build_tiered_router
    router = build_tiered_router()
    if router is None:
        print("❌ no LLM provider keys configured", file=sys.stderr)
        return 2

    r_issues = await smoke_rescue(router)
    a_issues = await smoke_augment(router)

    total = len(r_issues) + len(a_issues)
    print(f"\n=== Verdict ===")
    print(f"Rescue 問題數: {len(r_issues)}")
    print(f"Augment 問題數: {len(a_issues)}")
    if total == 0:
        print("✅ 全部輸出無偵測到簡體字 — prompt 工作正常")
        return 0
    print("❌ 偵測到簡體字混入：")
    for tag, key, out, bad in r_issues + a_issues:
        print(f"  [{tag}] {key} → {out!r} (簡體: {bad})")
    print("\n建議：強化 prompt（加更多負例 / 提高警告語氣 / 升 LLM tier 到 70b）")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
