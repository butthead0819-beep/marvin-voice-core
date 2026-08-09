"""tts_echo_guard.is_prompt_echo — LLM 把 prompt 複誦回來時擋掉 TTS。"""
from __future__ import annotations

from tts_echo_guard import is_prompt_echo


def test_verbatim_echo_blocked():
    prompt = "你是馬文。任務：玩家要求你唱歌，用憂鬱語氣抱怨人生，15字內。"
    response = "你是馬文。任務：玩家要求你唱歌，用憂鬱語氣抱怨人生，15字內。"
    assert is_prompt_echo(prompt, response) is True


def test_normal_reply_not_blocked():
    prompt = "你是馬文。任務：玩家要求你唱歌，用憂鬱語氣抱怨人生，15字內。"
    response = "唱歌？我連活著都嫌累。"
    assert is_prompt_echo(prompt, response) is False


def test_empty_strings_not_blocked():
    assert is_prompt_echo("", "") is False
    assert is_prompt_echo("some prompt", "") is False
    assert is_prompt_echo("", "some response") is False


def test_threshold_is_configurable():
    prompt = "馬文 唱歌 抱怨 人生"
    response = "馬文 唱歌 別的東西"
    # 高門檻不擋、低門檻擋
    assert is_prompt_echo(prompt, response, threshold=0.9) is False
    assert is_prompt_echo(prompt, response, threshold=0.1) is True
