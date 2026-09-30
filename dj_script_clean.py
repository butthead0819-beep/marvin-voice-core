"""清掉 LLM DJ 口白裡的雜訊（舞台指示括號、markdown 星號、怪引號、整段外層「」、換行）。

2026-09-30 使用者定：清雜訊後多長都完整播出，不截斷、不因超長退墊底。
"""
from __future__ import annotations

import re

_WHOLE_LINE_PAREN = re.compile(r"^[（(][^（）()]*[）)]$")
_LEADING_WS_NEWLINE = re.compile(r"\s*\n\s*")


def clean_dj_script(text: str) -> str:
    if not text:
        return ""

    text = text.replace("*", "").replace("„", "")

    lines = text.split("\n")
    lines = [line for line in lines if not _WHOLE_LINE_PAREN.match(line.strip())]
    text = "\n".join(lines)

    stripped = text.strip()
    if stripped.startswith("（") or stripped.startswith("("):
        close_char = "）" if stripped[0] == "（" else ")"
        close_idx = stripped.find(close_char)
        if close_idx != -1 and "《" not in stripped[:close_idx]:
            stripped = stripped[close_idx + 1:]
        text = stripped
    else:
        text = stripped

    text = _LEADING_WS_NEWLINE.sub("", text)
    text = text.strip()

    if text.startswith("「") and text.endswith("」"):
        inner = text[1:-1]
        if "「" not in inner and "」" not in inner:
            text = inner

    return text
