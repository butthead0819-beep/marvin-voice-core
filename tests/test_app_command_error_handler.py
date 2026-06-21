"""App command 全域錯誤處理器：失效 interaction(10062) 的優雅降級。

背景 incident（2026-06-21）：summon / marvin_play 的 defer() 偶因 event loop
lag 超過 Discord 3 秒 ACK 窗口，拋 NotFound 404 (10062 Unknown interaction)。
舊 handler 把它記為 ERROR（誤觸 incident 白名單）且又對失效 interaction
send_message 造成二次 404。本測試鎖定「辨識失效 interaction」的判斷邏輯。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from main_discord import _is_expired_interaction_error


def _make_not_found(code):
    resp = MagicMock()
    resp.status = 404
    return discord.NotFound(resp, {"code": code, "message": "x"})


def test_direct_notfound_10062_is_expired():
    assert _is_expired_interaction_error(_make_not_found(10062)) is True


def test_notfound_other_code_is_not_expired():
    # 10008 = Unknown message，不是 interaction 失效，不該被當成失效
    assert _is_expired_interaction_error(_make_not_found(10008)) is False


def test_wrapped_in_command_invoke_error_is_expired():
    # AppCommandError 常把真因包在 .original
    wrapped = SimpleNamespace(original=_make_not_found(10062))
    assert _is_expired_interaction_error(wrapped) is True


def test_generic_error_is_not_expired():
    assert _is_expired_interaction_error(RuntimeError("boom")) is False
