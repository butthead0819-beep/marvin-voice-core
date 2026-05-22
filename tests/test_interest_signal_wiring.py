"""
taste Phase C 接線：VoiceController._record_interest_signals 把明示偏好寫進 suki taste。

確定性偵測（taste_extractor）→ memory.record_taste_signal(speaker, item, ±delta)。
side-channel：任何例外都吞掉，不影響 utterance pipeline。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cogs.voice_controller import VoiceController
from taste_extractor import REALTIME_TASTE_DELTA


def _bare_cog():
    """繞過 heavy __init__：只裝 _record_interest_signals 需要的屬性。"""
    cog = VoiceController.__new__(VoiceController)
    cog.bot = MagicMock()
    cog.stt_logger = MagicMock()
    cog.bot.router.memory = MagicMock()
    return cog


def test_records_like_signal_to_taste():
    cog = _bare_cog()
    cog._record_interest_signals("大肚", "我超愛爬山")

    cog.bot.router.memory.record_taste_signal.assert_called_once()
    args, kwargs = cog.bot.router.memory.record_taste_signal.call_args
    assert args[0] == "大肚"
    assert args[1] == "爬山"
    assert args[2] == REALTIME_TASTE_DELTA


def test_records_dislike_signal_negative():
    cog = _bare_cog()
    cog._record_interest_signals("大肚", "我討厭香菜")

    args, _ = cog.bot.router.memory.record_taste_signal.call_args
    assert args[1] == "香菜"
    assert args[2] == -REALTIME_TASTE_DELTA


def test_no_signal_no_call():
    cog = _bare_cog()
    cog._record_interest_signals("大肚", "今天天氣不錯")

    cog.bot.router.memory.record_taste_signal.assert_not_called()


def test_exception_swallowed_does_not_raise():
    cog = _bare_cog()
    cog.bot.router.memory.record_taste_signal.side_effect = RuntimeError("db boom")
    # 不可拋出——side-channel 不能拖垮 utterance pipeline
    cog._record_interest_signals("大肚", "我喜歡貓")


def test_multiple_items_each_recorded():
    cog = _bare_cog()
    cog._record_interest_signals("大肚", "我喜歡爬山我討厭塞車")

    items = {c.args[1] for c in cog.bot.router.memory.record_taste_signal.call_args_list}
    assert items == {"爬山", "塞車"}
