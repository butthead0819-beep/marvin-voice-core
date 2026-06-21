"""故事編排：把 10 分鐘日誌（骨幹）+ 精華（高潮）融合成一頁漫畫的故事計畫。

設計（2026-06-21 與 Jack 定）：
- 條漫 off。有精華才出（沒高潮不畫）。
- 豐富（≥6 筆 context）→ 日漫 4 格：物件 context + Hero 斜切拆兩拍（鋪哏→爆笑）+ 標題 + 馬文。
- 薄 → 一格 meme：強反差單飛、反差中才 Marvin 救援。
- arc 編排：最強笑點當高潮（Hero），前面墊 context。

純函式（不碰 API）。實際出圖/清理/標題在 render 端注入 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from diary_comic.parser import DiaryEntry
from diary_comic.highlight import (
    Highlight, highlight_to_entry, meme_needs_marvin, _setup_text)

MIN_CONTEXT = 6  # ≥ 這麼多筆 → 漫畫；否則 meme


def choose_format(diary_session, highlights) -> str | None:
    """meme / slant / None。沒精華→None（不出）；豐富→slant；薄→meme。"""
    if not highlights:
        return None
    return "slant" if len(diary_session) >= MIN_CONTEXT else "meme"


@dataclass
class StoryPlan:
    format: str                                   # "meme" | "slant"
    highlight: Highlight                           # 高潮精華
    context: list[DiaryEntry] = field(default_factory=list)  # 物件 context（slant）
    peak_setup: DiaryEntry | None = None           # Hero 上格：鋪哏
    peak_reaction: DiaryEntry | None = None        # Hero 下格：爆笑
    meme_top: str = ""                             # meme 上文字（鋪哏）
    meme_bottom: str = ""                          # meme 下文字（Marvin 或空）
    needs_marvin: bool = False                     # meme 是否要 Marvin 救援


def fuse(diary_session, highlights, *, max_context: int = 2) -> StoryPlan | None:
    """融合成故事計畫。回 None = 不出。"""
    fmt = choose_format(diary_session, highlights)
    if fmt is None:
        return None
    peak = max(highlights, key=lambda h: h.strength)  # 最強笑點當高潮

    if fmt == "meme":
        need = meme_needs_marvin(peak)
        return StoryPlan(format="meme", highlight=peak,
                         meme_top=_setup_text(peak)[:30] or "（鋪哏）",
                         meme_bottom="" if not need else "",  # Marvin 文字 render 端生
                         needs_marvin=need)

    # slant：Hero 拆兩拍 + 物件 context（arc：context 在前、高潮在後）
    setup = highlight_to_entry(peak, core=_setup_text(peak)[:40] or "（鋪哏場景）")
    reaction = DiaryEntry(ts_str=setup.ts_str, core="全場哄堂大笑、爆笑反應",
                          speakers=setup.speakers, aside=peak.laugh_text)
    context = list(diary_session)[:max_context]  # 開場+鋪墊
    return StoryPlan(format="slant", highlight=peak, context=context,
                     peak_setup=setup, peak_reaction=reaction)


_TITLE_SYS = ("你是漫畫單話命名員。看這頁聊了什麼，取一個好笑、吸睛的單話標題"
              "（繁中、≤12 字、像漫畫章節名）。只回標題。")


def build_title_prompt(cores: list[str]) -> tuple[str, str]:
    bullets = "、".join(c for c in cores if c)
    return _TITLE_SYS, f"這頁的內容：{bullets}\n\n單話標題："
