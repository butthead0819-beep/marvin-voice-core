"""puck_gesture.py — 每則 Marvin 回覆開口時做一個頭部動作：動作與強度由程式決定，
LLM 回覆開頭可選加的 [[TAG]] 標籤只當參考（hint），不是唯一依據。

純邏輯（extract_gesture_tag / strip_gesture_stream / choose_reply_gesture /
PuckGestureDirector）+ 一個薄接點（on_speech，給 play_tts 呼叫）。
"""
from __future__ import annotations

import logging
import re
from collections import deque

import turn_outcome

logger = logging.getLogger(__name__)

GESTURE_CODES = ("T", "Y", "N", "G", "P")   # TILT / YES / NO / GLANCE / PONDER
_HINT_TO_CODE = {"TILT": "T", "YES": "Y", "NO": "N", "GLANCE": "G", "PONDER": "P"}
_TAG_RE = re.compile(r"\[\[\s*([A-Za-z]+)\s*\]\]")

GESTURE_PROMPT_DIRECTIVE = (
    "6. 回覆最前面加一個頭部動作標籤（不會被念出來）："
    "[[YES]] 同意、[[NO]] 否定、[[TILT]] 疑惑、[[GLANCE]] 不屑或敷衍、[[PONDER]] 思考，五選一。\n"
)


def extract_gesture_tag(text: str) -> tuple[str, str | None]:
    """移除 text 裡所有 [[XXX]]（不分大小寫、括號內可有空白）。回傳 (去標籤後 strip 的文字,
    第一個合法標籤對應的 code 或 None)。不合法標籤也會被移除，但不算 hint。"""
    hint: str | None = None

    def _repl(m: re.Match) -> str:
        nonlocal hint
        code = _HINT_TO_CODE.get(m.group(1).upper())
        if hint is None and code is not None:
            hint = code
        return ""

    cleaned = _TAG_RE.sub(_repl, text)
    return cleaned.strip(), hint


async def strip_gesture_stream(raw):
    """包在 LLM 串流外面的 async generator：剝掉 [[TAG]]，開頭的合法標籤記成本回合的
    gesture hint（turn_outcome.note_gesture_hint），中段/結尾的標籤只剝不記。"""
    header_mode = True
    header_buf = ""
    tail_buf = ""

    def _emit_normal(buf: str) -> tuple[str, str]:
        # 先剝完整標籤，再把尾端可能是標籤開頭的殘片（未閉合的 "[[…" 或單一 "["）留到下個 chunk。
        cleaned = _TAG_RE.sub("", buf)
        idx = cleaned.rfind("[[")
        if idx != -1 and "]]" not in cleaned[idx:] and len(cleaned) - idx <= 24:
            return cleaned[:idx], cleaned[idx:]
        if cleaned.endswith("["):
            return cleaned[:-1], "["
        return cleaned, ""

    async for chunk in raw:
        if chunk == "__SEARCHING__":
            yield chunk
            continue

        if header_mode:
            header_buf += chunk
            stripped = header_buf.lstrip()
            if stripped.startswith("[["):
                close = stripped.find("]]")
                if close != -1:
                    tag_part = stripped[: close + 2]
                    rest = stripped[close + 2:]
                    _, hint = extract_gesture_tag(tag_part)
                    if hint:
                        turn_outcome.note_gesture_hint(hint)
                    header_mode = False
                    header_buf = ""
                    tail_buf += rest
                elif len(stripped) > 24:
                    header_mode = False
                    tail_buf += header_buf
                    header_buf = ""
                else:
                    continue
            elif len(stripped) >= 2 or (len(stripped) == 1 and stripped != "["):
                header_mode = False
                tail_buf += header_buf
                header_buf = ""
            else:
                continue
        else:
            tail_buf += chunk

        if not header_mode:
            emit, tail_buf = _emit_normal(tail_buf)
            if emit:
                yield emit

    if header_mode:
        # 串流結束但還卡在開頭判斷階段（例如整串就是個沒閉合的短殘片）
        emit = _TAG_RE.sub("", header_buf)
    else:
        emit = _TAG_RE.sub("", tail_buf)
    if emit:
        yield emit


_STAGE_DIRECTION_RE = re.compile(r"^[（(]([^）)]{0,10})[）)]\s*")


def choose_reply_gesture(
    text: str, *, speaker: str | None, hint: str | None, hot_chat: bool, recent: list[str],
) -> tuple[str, int]:
    s = text.strip()
    if speaker:
        if s.lower().startswith(speaker.lower()):
            s = s[len(speaker):]
            if s and s[0] in "，,、 ":
                s = s[1:]
    m = _STAGE_DIRECTION_RE.match(s)
    while m:
        s = s[m.end():]
        m = _STAGE_DIRECTION_RE.match(s)
    s = s.strip()

    end_m = re.search(r"[。！？!?…\n]", s)
    first = s[: end_m.end()] if end_m else s

    if s.startswith("沒錯") or s.startswith("不錯"):
        code = "Y"
    elif s.startswith(("不", "沒", "別", "才不", "算了")):
        code = "N"
    elif s.startswith(("對", "好", "是", "嗯", "可以", "當然", "行")) and not s.startswith("好像"):
        code = "Y"
    elif first.endswith("？") or first.endswith("?"):
        code = "T"
    elif any(k in text for k in ("（嘆氣）", "(嘆氣)", "唉", "隨便", "無所謂")):
        code = "G"
    elif len(s) >= 40:
        code = "P"
    elif hint in GESTURE_CODES:
        code = hint
    else:
        code = "T"
        for c in ("T", "Y", "G", "P"):
            if c not in recent:
                code = c
                break

    if "！" in text or "!" in text or hot_chat:
        level = 3
    elif "…" in text or "..." in text or "嘆氣" in text or "唉" in text:
        level = 1
    else:
        level = 2

    return code, level


class PuckGestureDirector:
    def __init__(self) -> None:
        self._seq = 0
        self._recent: "deque[str]" = deque(maxlen=2)
        self._field: str | None = None

    def publish(self, code: str, level: int) -> None:
        self._seq += 1
        if self._seq > 9999:
            self._seq = 1
        self._recent.append(code)
        self._field = f"{self._seq}:{code}{level}"

    @property
    def recent(self) -> list[str]:
        return list(self._recent)

    @property
    def field(self) -> str | None:
        return self._field


def on_speech(vc, text: str, voice) -> None:
    """給 play_tts 呼叫的薄接點：本回合第一句（voice is None）才決定並 publish 動作。"""
    try:
        if voice is not None:
            return
        rec = turn_outcome.claim_first_speech()
        if rec is None:
            return
        director = getattr(vc, "_puck_gesture_director", None)
        if director is None:
            director = PuckGestureDirector()
            setattr(vc, "_puck_gesture_director", director)
        try:
            hot = bool(vc._room_mood_store.get(0).hot_chat)
        except Exception:
            hot = False
        code, level = choose_reply_gesture(
            text,
            speaker=rec.get("speaker"),
            hint=rec.get("_gesture_hint"),
            hot_chat=hot,
            recent=director.recent,
        )
        director.publish(code, level)
        logger.info(f"🗿 [PuckGesture] {code}{level} hint={rec.get('_gesture_hint')} '{text[:30]}'")
    except Exception as e:
        logger.debug(f"[PuckGesture] on_speech 失敗（忽略）: {e}")
