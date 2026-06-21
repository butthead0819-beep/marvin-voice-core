"""出圖時現生一句馬文吐槽（page-level punchline）。

6 月日記已無【碎念】，改成拼版時把整頁核心丟 LLM 現生一句馬文毒舌，當這頁的笑點。
LLM 注入式：generate_fn(system, user) -> str，production 接 gemini/groq，測試接假的。
"""
from __future__ import annotations

from typing import Callable

GenerateFn = Callable[[str, str], str]

_SYSTEM = (
    "你是馬文，一個厭世、毒舌但好笑的 AI 旁觀者。"
    "你會看一群朋友這一小時的對話重點，然後用一句話酸他們。"
    "規則：只回那一句話，繁體中文，30 字以內，毒但不惡意，要好笑。"
)


def build_prompt(cores: list[str]) -> tuple[str, str]:
    """組 (system, user)。user 帶這一頁所有核心。"""
    bullets = "\n".join(f"- {c}" for c in cores)
    user = f"這一小時他們聊了這些：\n{bullets}\n\n用一句話吐槽（≤30字，只回那句）："
    return _SYSTEM, user


def generate_page_punchline(cores: list[str],
                            generate_fn: GenerateFn | None = None) -> str:
    """生一句馬文吐槽。無 cores 或無 LLM → 留白；LLM 失敗 → 降級留白（不炸拼版）。"""
    if not cores or generate_fn is None:
        return ""
    system, user = build_prompt(cores)
    try:
        return (generate_fn(system, user) or "").strip()
    except Exception:
        return ""
