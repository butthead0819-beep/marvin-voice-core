import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from cogs.voice_controller import VoiceController


class EmptyBuffer:
    def get_harvest(self, *_args, **_kwargs):
        return ""


def make_controller():
    controller = VoiceController.__new__(VoiceController)
    controller.speaker_dialogue_states = {}
    controller.speech_buffers = {}
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_text, expected",
    [
        ("馬文下一首", "下一首"),   # 3 字 skip 指令，曾被 <4 字閘吞掉
        ("馬文繼續播", "繼續播"),   # 3 字 resume 指令，同一個 bug
    ],
)
async def test_confirmation_accepts_short_control_command_without_waiting(initial_text, expected):
    """Regression: 「馬文下一首」剝詞後剩「下一首」(3 字)，曾被 _confirmation_flow
    的 len<4 字閘當成『只喊了喚醒詞』，去等後續問句→10s 逾時→「沒聽清楚」，
    指令從未送到 IntentBus。短控制指令本身即完整指令，必須即時返回。"""
    controller = make_controller()

    query = await controller._confirmation_flow("User1", 123.0, initial_text=initial_text)

    assert query == expected
    assert controller.speaker_dialogue_states == {}


@pytest.mark.asyncio
async def test_confirmation_wait_uses_named_timeout_constant(monkeypatch):
    """只喊喚醒詞→等後續問句的逾時秒數必須用 _CONFIRM_WAIT_TIMEOUT 常數（不可再寫死 10.0），
    且值已由 10s 降到 4s，縮短單 worker 佇列尾巴（一人的思考停頓不再霸佔整個 worker 10 秒）。"""
    controller = make_controller()
    controller.play_tts = AsyncMock()

    captured = {}

    async def fake_wait_for(coro, timeout):
        captured["timeout"] = timeout
        coro.close()  # 關掉未 await 的 evt.wait() coroutine，避免 warning
        raise asyncio.TimeoutError

    monkeypatch.setattr("cogs.voice_controller.asyncio.wait_for", fake_wait_for)

    # initial_text 只有喚醒詞 → stripped < 4 → 進入等問句分支
    query = await controller._confirmation_flow("User1", 123.0, initial_text="馬文")

    assert query is None                                              # 逾時 → 回 None
    assert captured["timeout"] == VoiceController._CONFIRM_WAIT_TIMEOUT
    assert VoiceController._CONFIRM_WAIT_TIMEOUT == 4.0


@pytest.mark.asyncio
async def test_process_queued_query_passes_speaker_to_harvest():
    """Regression: get_harvest must receive speaker= so cross-talk doesn't pollute queries."""
    controller = make_controller()
    harvest_mock = MagicMock(return_value="")
    controller.bot.engine.conv_buffer.get_harvest = harvest_mock
    controller.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])

    await controller._process_queued_query("Alice", wake_time=100.0)

    harvest_mock.assert_called_once_with(100.0, before=3.0, after=1.0, speaker="Alice")
