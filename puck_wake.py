"""puck 無 prompt 喚醒偵測：拼音比對（馬文/馬威/馬尾/馬溫 等 Whisper 誤聽變體）+ 英文 marvin。

背景見任務 2a：拿掉 WakeStreamDetector 的 initial_prompt 後 Whisper 聽到的中文喚醒詞
會是「馬」接近音字（文/威/溫/萬），用拼音比對取代字面比對來吃掉這些誤聽變體。
"""
from __future__ import annotations

import re

from pypinyin import lazy_pinyin

_CJK_RE = re.compile(r"[一-鿿]")
_MARVIN_RE = re.compile(r"marvin", re.IGNORECASE)
_MA_SECOND_SYLLABLES = {"wen", "wei", "weng", "wan"}
_LEADING_PUNCT_RE = re.compile(r"^[，,、。.！!？?\s]+")


def pinyin_wake_match(text: str) -> int | None:
    """回傳喚醒詞結束位置（原字串 index），沒命中回 None。"""
    end = None

    cjk_positions = [i for i, ch in enumerate(text) if _CJK_RE.match(ch)]
    if cjk_positions:
        # 逐字轉拼音，避免 lazy_pinyin 用詞組上下文（跨 gap 黏成不存在的詞）影響多音字判斷
        pinyins = [lazy_pinyin(text[i])[0] for i in cjk_positions]
        for k in range(len(cjk_positions) - 1):
            i, j = cjk_positions[k], cjk_positions[k + 1]
            # 相鄰：中間只能是標點/空白，不能隔其他字元（含 ASCII 字母數字）
            gap = text[i + 1:j]
            if gap and not all(not ch.isalnum() for ch in gap):
                continue
            if pinyins[k] == "ma" and pinyins[k + 1] in _MA_SECOND_SYLLABLES:
                end = j + 1
                break

    m = _MARVIN_RE.search(text)
    if m and (end is None or m.end() < end):
        end = m.end()

    return end


def strip_through_wake(text: str) -> str:
    """去掉喚醒詞前綴，回傳剩餘內容（去除開頭標點/空白）。"""
    end = pinyin_wake_match(text)
    if end is None:
        return text.strip()
    return _LEADING_PUNCT_RE.sub("", text[end:])
