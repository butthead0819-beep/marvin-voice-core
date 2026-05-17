from __future__ import annotations
import logging
import re

logger = logging.getLogger(__name__)

_HINT_STRIP_RE = re.compile(r"[「」『』【】《》〈〉\{\}]")


def _sanitize_hint(hint: str) -> str:
    """Strip characters that could break the LLM system-prompt template."""
    return _HINT_STRIP_RE.sub("", hint).replace("\n", " ").replace("\r", " ")

# ── Per-round revelation levels ────────────────────────────────────────────────
# Each level describes WHAT DIMENSION of the answer to hint at.
# Round 1 is the most cryptic; round 5 is nearly direct.

_LEVEL_INSTRUCTIONS = {
    1: (
        "【第一條線索 — 本質/感覺】"
        "只描述它的「本質感覺」，用比喻或詩意的方式表達。"
        "聽者應該完全摸不著頭緒，感覺神秘。"
    ),
    2: (
        "【第二條線索 — 功能/用途】"
        "描述它的「功能」或「用途」，讓人知道它能做什麼。"
        "完全不提外形、顏色或任何視覺特徵。"
    ),
    3: (
        "【第三條線索 — 外形/場景】"
        "可以提到它的「外形、材質、使用場景或所屬類別」。"
        "開始讓人有跡可循，但仍不能太直白。"
    ),
    4: (
        "【第四條線索 — 明顯特徵】"
        "說出一個非常明顯的「識別特徵」或「與它直接相關的事物」。"
        "讓猜題人感覺快要猜到了。"
    ),
    5: (
        "【第五條線索 — 最後提示】"
        "幾乎要把答案說出來，只差最後一步。"
        "給出最關鍵、最直接的描述，讓人有機會猜中。"
    ),
}

_CLUE_SYSTEM = """你是猜謎遊戲的出題助手。
答案是「{answer}」（共 {char_count} 個字）。
{theme_section}{hint_section}{prior_section}
{level_instruction}

規則：
- 絕對不可直接說出答案本身或諧音
- 不可以使用答案裡的任何字
- 只輸出這一條線索的文字，不加編號或說明
"""


async def generate_clue(
    answer: str,
    round_num: int,
    prior_clues: list[str],
    router,
    *,
    theme: str | None = None,
    setter_hint: str | None = None,
) -> str:
    """
    Generate the clue for the given round.

    round_num: 1–5 (1 = first/most cryptic, 5 = near-direct)
    router: GeminiRouter instance with .complete(system, user) method.
    Returns a single clue string.
    """
    char_count = len(answer)
    level = max(1, min(5, round_num))
    level_instruction = _LEVEL_INSTRUCTIONS[level]

    theme_section = f"本輪主題：「{theme}」\n" if theme else ""
    hint_section = (
        f"出題者的提示：「{_sanitize_hint(setter_hint)}」（請在線索中融入這個方向）\n"
        if setter_hint
        else ""
    )

    if prior_clues:
        prior_section = "已有線索（新線索不可重複同一角度）：\n" + "\n".join(
            f"  線索{i+1}：{c}" for i, c in enumerate(prior_clues)
        )
    else:
        prior_section = ""

    system = _CLUE_SYSTEM.format(
        answer=answer,
        char_count=char_count,
        theme_section=theme_section,
        hint_section=hint_section,
        prior_section=prior_section,
        level_instruction=level_instruction,
    )
    user = f"請給出第 {round_num} 條線索。"

    try:
        first = await router.complete(system=system, user=user)
    except Exception as e:
        logger.error(f"[CluGen] LLM failed: {e}")
        return "（線索生成失敗，繼續猜吧！）"

    if not _leaks_answer(first, answer):
        return first

    # Retry once with a stricter reminder. Prompts that say "don't" sometimes
    # land better when we acknowledge the prior miss.
    retry_system = system + "\n⚠ 上一次嘗試洩漏了答案字，請務必避開答案中的每一個字。"
    try:
        second = await router.complete(system=retry_system, user=user)
    except Exception as e:
        logger.error(f"[CluGen] LLM retry failed: {e}")
        return "（線索生成失敗，繼續猜吧！）"

    if not _leaks_answer(second, answer):
        return second

    logger.warning(
        "[CluGen] clue leaked answer chars on both attempts (answer=%r), using safe fallback",
        answer,
    )
    return "（這條線索略過，看下一條吧！）"


def _leaks_answer(clue: str, answer: str) -> bool:
    """Return True if any character of the answer appears in the clue."""
    return any(c in clue for c in answer)


def judge_answer(answer: str, guess: str) -> bool:
    """Exact string match (case-insensitive, strip whitespace). Rounds 1–4 only."""
    return answer.strip().lower() == guess.strip().lower()
