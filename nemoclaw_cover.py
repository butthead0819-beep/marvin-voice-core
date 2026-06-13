"""NemoClaw 掩飾語（cover/stall）— 遮掩 openclaw agent 的 3-12s thinking。

設計（2026-06-13，實測收斂）：openclaw agent 延遲外部不可控（3-12s）。龍蝦要正確
答案 → 不能用快 LLM 替換答案（會幻覺）。改用快 LLM 生掩飾語遮掩開頭死寂。

關鍵設計（使用者拍板）：**LLM 只產「句型框架」，主體由我們從原句套進去。**
實測證明「LLM 自由換句話說」會掰事實（�BrecyclerView出車手名「蕭亞斯」），且 CJK 改寫與
掰名在 bigram 層分不開、護欄擋不乾淨。改成 LLM 出含 `{Q}` 佔位的自然框架（它碰不到
主體 → 結構上不可能洩漏答案），主體用原句的詞確定性插入 → 自然度來自框架變化、
安全來自主體永遠是原句。框架本身再過數字/英文 backstop。
"""
from __future__ import annotations

import random
import re
from typing import Awaitable, Callable, Optional

_PLACEHOLDER = "{Q}"

# 主體前綴的命令詞（套進框架前剝掉，讓主體更乾淨、不跟框架的「我來查」重複）
_LEAD_CMD_RE = re.compile(r"^(幫我|請|麻煩|幫忙|我想|想)?(查一下|查查|查詢|查|找一下|找找|找|問)?\s*")


def _numbers(s: str) -> set[str]:
    out = set(re.findall(r"\d+", s))
    out |= set(re.findall(r"[零一二三四五六七八九十百千萬億兆]{2,}", s))
    return out


def _latin(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def extract_subject(clean_query: str) -> str:
    """從(已去 trigger 的)query 取主體：剝開頭命令詞、去尾標點。回原句的詞（安全）。"""
    s = _LEAD_CMD_RE.sub("", clean_query.strip(), count=1).strip()
    s = s.rstrip("？?。.！! ，,、啊呢嗎")  # 去尾標點 + 句尾語氣詞（吧/喔曖昧:酒吧/網吧，不砍）
    return s or clean_query.strip().rstrip("？?。.！! ")


# 靜態框架池（LLM 失敗 / 不可用時用；天生安全，只有框架 + {Q}）
_STATIC_FRAMES = (
    f"好問題，{_PLACEHOLDER}，我來查查看。",
    f"這個嘛，{_PLACEHOLDER}，讓我查一下。",
    f"等我一下，{_PLACEHOLDER}，我馬上查。",
    f"嗯，{_PLACEHOLDER}，這個我來找找。",
    f"好，{_PLACEHOLDER}，讓我看看資料。",
)


def frame_is_safe(frame: str) -> bool:
    """框架合格檢查：恰一個 {Q}，且框架本身（去 {Q}）無數字/英文（LLM 別在框架塞事實）。"""
    if not frame or frame.count(_PLACEHOLDER) != 1:
        return False
    shell = frame.replace(_PLACEHOLDER, "")
    if _numbers(shell) or _latin(shell):
        return False
    if len(shell) > 30:   # 框架應短；過長多半是 LLM 沒照格式
        return False
    return True


def build_cover(frame: str, subject: str) -> Optional[str]:
    """把主體套進框架。框架不合格回 None（caller 退 fallback）。"""
    if not frame_is_safe(frame) or not subject.strip():
        return None
    return frame.replace(_PLACEHOLDER, subject.strip())


def safe_fallback_cover(subject: str) -> str:
    """靜態框架 + 主體（天生不洩漏：框架固定、主體來自原句）。"""
    subj = subject.strip() or "你問的問題"
    return random.choice(_STATIC_FRAMES).replace(_PLACEHOLDER, subj)


async def generate_cover(
    clean_query: str,
    llm_fn: Callable[[str, str], Awaitable[Optional[str]]],
) -> str:
    """產生掩飾語：LLM 出句型框架(含{Q}) → 套主體 → 不合格退靜態框架。回保證安全的掩飾語。"""
    subject = extract_subject(clean_query)
    try:
        frame = await llm_fn(_FRAME_SYSTEM, clean_query)
    except Exception:
        frame = None
    if frame:
        cover = build_cover(frame.strip().strip("「」\"' "), subject)
        if cover:
            return cover
    return safe_fallback_cover(subject)


_FRAME_SYSTEM = (
    "你是語音助理馬文的「掩飾語句型」產生器。使用者問了需要查證的問題，真答案由背景查"
    "（需幾秒）。你只負責產生一句自然的拖延句型，用 {Q} 當佔位符代表使用者的問題，"
    "表示馬文聽懂了、正在查。\n"
    "鐵則：\n"
    "1. 句子裡必須恰好出現一次 {Q}（佔位符，之後由系統填入問題）。\n"
    "2. {Q} 以外只能有自然的口語框架（開場 + 表示在查），語氣可變化。\n"
    "3. 絕對禁止：任何具體內容、事實、數字、人名、答案、猜測——你不知道問題是什麼，"
    "只是產生句型。\n"
    "範例：好問題，{Q}，讓我查一下。 / 這個嘛，{Q}，我馬上找找。\n"
    "口語繁體中文，一句話，不要引號。"
)
