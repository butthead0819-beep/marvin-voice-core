import pytest
from unittest.mock import MagicMock

from cogs.voice_controller import VoiceController


class EmptyBuffer:
    def get_harvest(self, *_args, **_kwargs):
        return ""


def make_controller():
    controller = VoiceController.__new__(VoiceController)
    controller.speaker_dialogue_states = {}
    controller.bot = MagicMock()
    controller.bot.engine = MagicMock()
    controller.bot.engine.conv_buffer = EmptyBuffer()
    controller.bot.router = MagicMock()
    return controller


def test_strip_wake_word_prefers_longest_and_is_case_insensitive():
    controller = make_controller()

    assert controller._strip_wake_word("馬文同學，幫我看一下") == "幫我看一下"
    assert controller._strip_wake_word("嗨Mom 你覺得呢") == "你覺得呢"
    assert controller._strip_wake_word("MARVIN, play this") == "play this"


def test_query_quality_gate_rejects_empty_wake_and_accepts_real_question():
    controller = make_controller()

    assert controller._query_quality_gate("馬文")[0] is False
    assert controller._query_quality_gate("馬文，那個")[0] is False
    assert controller._query_quality_gate("馬文，幫我看一下這個畫面")[0] is True


def test_low_confidence_answer_detection_blocks_weak_llm_text():
    controller = make_controller()

    assert controller._is_low_confidence_answer("[SKIP]") is True
    assert controller._is_low_confidence_answer("我不確定你在問什麼") is True
    assert controller._is_low_confidence_answer("紅色那個先打掉，旁邊有補包。") is False


@pytest.mark.asyncio
async def test_confirmation_uses_initial_wake_text_query_without_waiting():
    controller = make_controller()

    query = await controller._confirmation_flow(
        "User1",
        123.0,
        initial_text="馬文，幫我看一下這個畫面",
    )

    assert query == "幫我看一下這個畫面"
    assert controller.speaker_dialogue_states == {}
