"""
barge_in —— 對談窗內「提問者打斷 Marvin」的純核心邏輯。

零 IO：不呼叫 time.time()，`now` 一律由呼叫端傳入。接線（watcher task / mixer /
TTS）在 cogs/voice_controller_wake_session.py 與 cogs/voice_controller.py。
"""
from __future__ import annotations

import re

BARGE_CONFIRM_S = 0.8      # target 持續講話超過這麼久才真的切
DIP_GAP_S = 0.4             # 確認前沉默超過這麼久 → 只是短促回應（嗯/對/咳嗽），放棄
USER_DONE_GAP_S = 0.7       # 切掉後，target 沉默超過這麼久 → 算講完，播 ack
ACK_WAIT_MAX_S = 15.0       # 等 target 講完的上限，超過就不播 ack
BARGE_PENDING_TTL_S = 20.0  # session 的 barge_in_pending 過期時間
CONFIRM_TIMEOUT_S = 3.0
CONFIRM_MAX_CHARS = 30
INTERRUPTED_TEXT_MAX_CHARS = 300
REPLY_LINES_WINDOW_S = 60.0
BARGE_ACK_FILES = ("assets/acks/ack_7.mp3", "assets/acks/ack_9.mp3", "assets/acks/ack_2.mp3")

_QUOTE_CHARS = "「」『』“”‘’\""
_SENTENCE_END_RE = re.compile(r"[。！？!?\n]")


def confirm_step(onset: float, now: float, last_spoken: float) -> str:
    """回 "cut" / "dip" / "wait"。"""
    if now - last_spoken >= DIP_GAP_S:
        return "dip"
    if now - onset >= BARGE_CONFIRM_S:
        return "cut"
    return "wait"


def user_done(now: float, last_spoken: float) -> bool:
    return now - last_spoken >= USER_DONE_GAP_S


def interrupted_text(lines, now: float) -> str:
    text = "".join(t for ts, t in lines if now - ts <= REPLY_LINES_WINDOW_S)
    if len(text) > INTERRUPTED_TEXT_MAX_CHARS:
        text = text[:INTERRUPTED_TEXT_MAX_CHARS]
    return text


def clean_confirm(raw) -> str:
    """LLM 確認句清理：非字串/空白/「略」→ ""；去引號；只取第一句；過長 → ""。"""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text or text == "略":
        return ""
    text = text.strip(_QUOTE_CHARS).strip()
    m = _SENTENCE_END_RE.search(text)
    if m:
        text = text[:m.start()] if text[m.start()] == "\n" else text[:m.start() + 1]
    text = text.strip()
    if not text or text == "略":
        return ""
    if len(text) > CONFIRM_MAX_CHARS:
        return ""
    return text
