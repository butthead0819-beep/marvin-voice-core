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

    # 🃏 長答案內含搪塞詞（笑話/引述的哏）不該被誤判為低信心——搪塞詞是內容不是馬文搪塞。
    # 7/5 User_local live：笑話被 _is_low_confidence_answer 吞掉不發聲的根因。
    assert controller._is_low_confidence_answer(
        "有一台機器人問宇宙的意義，宇宙回答：「我不知道，但我建議你先去洗碗。」") is False
    assert controller._is_low_confidence_answer(
        "我覺得這題超有趣的，雖然有些人會說不知道答案，但我認為就是四十二啦") is False  # 「不知道」埋句中
    # 但「以搪塞詞開頭的長答案」仍算低信心（馬文真的在搪塞）
    assert controller._is_low_confidence_answer(
        "我不知道你在說什麼欸，可以再講清楚一點嗎拜託") is True


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


def test_detect_music_direct_command_fuzzy_fallback():
    """IBA-T0 無喚醒詞：糊字控制指令走拼音兜底（精確表 miss → fuzzy 救回）。"""
    controller = make_controller()
    assert controller._detect_music_direct_command("下一手") == {"action": "skip"}
    assert controller._detect_music_direct_command("切鴿") == {"action": "skip"}
    assert controller._detect_music_direct_command("繼續撥") == {"action": "resume"}
    # 閒聊/問句不可誤觸
    assert controller._detect_music_direct_command("為什麼要一直跳過這首歌") is None


def test_detect_music_command_fuzzy_fallback():
    """wake 版偵測器同樣拼音兜底（供 confirmation_flow 的短命令 wait 判定）。"""
    controller = make_controller()
    assert controller._detect_music_command("下一手") == "skip"
    assert controller._detect_music_command("你今天好嗎") is None


@pytest.mark.asyncio
async def test_confirmation_normalizes_garbled_command_skipping_cleaner():
    """糊字短控制指令「馬文下一手」→ 剝喚醒詞「下一手」→ 拼音正規化「下一首」直接返回，
    不進 cleaner LLM（下游 PlaybackControlAgent regex 命中）。"""
    controller = make_controller()
    query = await controller._confirmation_flow("User1", 123.0, initial_text="馬文下一手")
    assert query == "下一首"


@pytest.mark.asyncio
async def test_process_queued_query_passes_speaker_to_harvest():
    """Regression: get_harvest must receive speaker= so cross-talk doesn't pollute queries."""
    controller = make_controller()
    harvest_mock = MagicMock(return_value="")
    controller.bot.engine.conv_buffer.get_harvest = harvest_mock
    controller.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])

    await controller._process_queued_query("Alice", wake_time=100.0)

    harvest_mock.assert_called_once_with(100.0, before=3.0, after=1.0, speaker="Alice")
