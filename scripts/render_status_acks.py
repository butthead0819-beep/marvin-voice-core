#!/usr/bin/env python3
"""渲染 LLM 處理中狀態 ack 語音（≤5 字，厭世口吻）到 assets/acks_status/。

wake 後 LLM gap ≥5s 才有 ack 價值（<5s 插話只是吵）。雙發：5s first / 12s second。
4 狀態 × 2 tier × 2 變體 = 16 個 mp3。預渲染 → 瞬間出聲、不依賴即時 TTS/LLM。

用 Marvin 本人聲音（zh-TW-YunJheNeural, rate -20%, pitch -15Hz）。
重跑只補缺檔（已存在跳過）。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import edge_tts

VOICE = os.getenv("TTS_VOICE", "zh-TW-YunJheNeural")
RATE = "-20%"
PITCH = "-15Hz"
OUT_DIR = Path(__file__).resolve().parents[1] / "assets" / "acks_status"

# state → tier → [變體]（≤5 字）
ACKS: dict[str, dict[str, list[str]]] = {
    "thinking": {   # 泛用「還沒回」
        "first":  ["等我想想", "容我一下"],
        "second": ["還在想", "快好了"],
    },
    "searching": {  # 在查網路資料
        "first":  ["查資料中", "我去查"],
        "second": ["還在查", "快查到"],
    },
    "busy": {       # LLM 線路塞 / 429 排隊
        "first":  ["線路塞爆", "排隊中"],
        "second": ["還在排", "快輪到"],
    },
    "fallback": {   # 切備援核心
        "first":  ["切備援腦", "降級中"],
        "second": ["備援頂著", "將就用"],
    },
}


async def _render(text: str, path: Path) -> None:
    comm = edge_tts.Communicate(text=text, voice=VOICE, rate=RATE, pitch=PITCH)
    await comm.save(str(path))


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    made, skipped = 0, 0
    for state, tiers in ACKS.items():
        for tier, variants in tiers.items():
            for i, text in enumerate(variants, 1):
                path = OUT_DIR / f"{state}_{tier}_{i}.mp3"
                if path.exists() and path.stat().st_size > 100:
                    skipped += 1
                    continue
                await _render(text, path)
                made += 1
                print(f"  ✅ {path.name}: 「{text}」")
    print(f"\n完成：新生 {made}、跳過 {skipped}。輸出 {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
