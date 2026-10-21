"""TranscriptMirror — STTHistory → Discord 文字頻道鏡像（10/21，穩定前要在文字頻道看到 STT / 回答 / 用了哪個 LLM）。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from transcript_mirror import TranscriptMirror, format_mirror_line, install_transcript_mirror


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("STTHistory", logging.INFO, "", 0, msg, None, None)


# ── format_mirror_line ──────────────────────────────────────────────────────

def test_format_wake_line_updates_last_raw():
    last_raw: dict[str, str] = {}
    line = format_mirror_line(
        "[⚡喚醒] [showay] raw='馬文你知道這個遊戲嗎' | Track=A | wake_intent=None", last_raw,
    )
    assert line is not None
    assert "showay" in line
    assert "馬文你知道這個遊戲嗎" in line
    assert last_raw["showay"] == "馬文你知道這個遊戲嗎"


def test_format_query_line_same_as_raw_returns_none():
    last_raw = {"showay": "馬文你知道這個遊戲嗎"}
    line = format_mirror_line(
        "[✅Query通過] [showay] gate_ok | query='你知道這個遊戲嗎'", last_raw,
    )
    assert line is None


def test_format_query_line_different_from_raw_shown():
    last_raw = {"showay": "人民區的傳送點怎麼開"}
    line = format_mirror_line(
        "[✅Query通過] [showay] gate_ok | query='人民廣場的傳送點怎麼開'", last_raw,
    )
    assert line is not None
    assert "清理後" in line
    assert "人民廣場" in line


def test_format_bot_line_includes_tag_and_text():
    last_raw: dict[str, str] = {}
    line = format_mirror_line(
        "[BOT→showay] (AmbientQA | gemini-3.5-flash付費 搜尋2次 4.2s 上下文:問答1輪/背景3句) 答案在這",
        last_raw,
    )
    assert line is not None
    assert "gemini-3.5-flash付費" in line
    assert "答案在這" in line


def test_format_unrelated_line_returns_none():
    last_raw: dict[str, str] = {}
    assert format_mirror_line("[showay] (Debounced) 隨便聊", last_raw) is None


# ── TranscriptMirror ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mirror_batches_multiple_emits_into_one_send():
    loop = asyncio.get_running_loop()
    sent = []

    class _FakeChannel:
        async def send(self, text, allowed_mentions=None):
            sent.append(text)

    channel = _FakeChannel()
    mirror = TranscriptMirror(loop, lambda: channel, flush_s=0.01)

    mirror.emit(_record("[⚡喚醒] [showay] raw='第一句' | Track=A | wake_intent=None"))
    mirror.emit(_record("[✅Query通過] [showay] gate_ok | query='完全不同的句子'"))
    mirror.emit(_record("[BOT→showay] (AmbientQA) 答案"))

    await asyncio.sleep(0.05)

    assert len(sent) == 1
    assert "第一句" in sent[0]
    assert "完全不同的句子" in sent[0]
    assert "答案" in sent[0]


@pytest.mark.asyncio
async def test_mirror_no_channel_does_not_send():
    loop = asyncio.get_running_loop()
    mirror = TranscriptMirror(loop, lambda: None, flush_s=0.01)
    mirror.emit(_record("[⚡喚醒] [showay] raw='測試' | Track=A | wake_intent=None"))
    await asyncio.sleep(0.05)
    # 沒有 channel，不會炸也不會送（沒有 assert 對象，只要不拋例外就過）


@pytest.mark.asyncio
async def test_mirror_splits_long_batch_into_multiple_messages():
    loop = asyncio.get_running_loop()
    sent = []

    class _FakeChannel:
        async def send(self, text, allowed_mentions=None):
            sent.append(text)

    channel = _FakeChannel()
    mirror = TranscriptMirror(loop, lambda: channel, flush_s=0.01)

    long_text = "字" * 1000
    for i in range(5):
        mirror.emit(_record(f"[⚡喚醒] [showay] raw='{long_text}{i}' | Track=A | wake_intent=None"))

    await asyncio.sleep(0.05)

    assert len(sent) > 1
    for msg in sent:
        assert len(msg) <= 1900


@pytest.mark.asyncio
async def test_mirror_send_exception_does_not_raise():
    loop = asyncio.get_running_loop()

    class _FailingChannel:
        async def send(self, text, allowed_mentions=None):
            raise RuntimeError("discord down")

    mirror = TranscriptMirror(loop, lambda: _FailingChannel(), flush_s=0.01)
    mirror.emit(_record("[⚡喚醒] [showay] raw='測試' | Track=A | wake_intent=None"))
    await asyncio.sleep(0.05)
    # 不拋例外即過


@pytest.mark.asyncio
async def test_install_transcript_mirror_idempotent():
    stt_logger = logging.getLogger("STTHistory")
    before = [h for h in stt_logger.handlers if isinstance(h, TranscriptMirror)]
    loop = asyncio.get_running_loop()
    try:
        h1 = install_transcript_mirror(loop, lambda: None)
        h2 = install_transcript_mirror(loop, lambda: None)
        assert h1 is h2
        after = [h for h in stt_logger.handlers if isinstance(h, TranscriptMirror)]
        assert len(after) == len(before) + 1
    finally:
        for h in list(stt_logger.handlers):
            if isinstance(h, TranscriptMirror) and h not in before:
                stt_logger.removeHandler(h)
