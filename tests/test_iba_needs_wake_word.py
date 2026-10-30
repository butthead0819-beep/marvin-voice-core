"""IBA 判喚醒也要真的叫名字（2026-10-24 使用者定：需要喚醒詞才放行，但調整 regex）。

一週 12 次 IBA wake=True 且 v=0.3（沒喚醒詞）：「冰充電並播放啊」「我能幫你查一下」
「等他跟第一次會同步換歌嗎」等誤觸發，夾著幾句喚醒詞被聽錯（法文/打文）的真指令。
v≥0.95 的 53 次全都含喚醒詞 → 以 voice 通道判；聽錯版只認「句首 + 緊接指令詞」。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from wake_detector import has_mangled_wake


@pytest.mark.parametrize("text", [
    "法文繼續播放啊", "打文播放陶晶瑩的養你一輩子", "保文幫我查遊戲叫做微小生存戰二",
    "南文幫我查光烈兔的綠春", "保溫停止播放", "欸，法文下一首",   # 30 天內實際聽錯的喚醒詞
    "麻煩下一首", "打完播放左邊的人", "拿我下一首", "老媽播放晴天", "媽我下一首",
])
def test_mangled_wake_at_start_before_command(text):
    assert has_mangled_wake(text)


@pytest.mark.parametrize("text", [
    "我最近在學法文", "法文很難念", "保溫杯放哪裡", "冰充電並播放啊", "播放酷狗音樂",
    "你說我一血啊我能幫你查一下", "中文幫我查一下", "",
    "麻煩你幫我看一下", "我打完了",
])
def test_not_mangled_wake(text):
    assert not has_mangled_wake(text)


def test_strip_mangled_wake_helper():
    from wake_detector import strip_mangled_wake
    assert strip_mangled_wake("麻煩下一首") == "下一首"
    assert strip_mangled_wake("打完播放左邊的人") == "播放左邊的人"
    assert strip_mangled_wake("拿我下一首") == "下一首"
    assert strip_mangled_wake("今天天氣很好") == "今天天氣很好"


# ── WakeSessionMixin._iba_wake_word_ok ──────────────────────────────────────

from cogs.voice_controller_wake_session import WakeSessionMixin


class _Cog(WakeSessionMixin):
    _WAKE_PATTERNS = ["馬文", "老馬"]

    def __init__(self):
        self._wake_session = None


def _ch(voice, alt=0.0):
    return {"voice": voice, "task": 0.85, "info": 0.0, "control": 0.9, "alt_wake": alt,
            "threshold": 0.32, "total": 0.57}


@pytest.mark.parametrize("text,ch,expected", [
    ("冰充電並播放啊", _ch(0.3), False),          # 10/23 前誤觸發
    ("我能幫你查一下", _ch(0.3), False),
    ("馬文下一首", _ch(1.0), True),
    ("哎馬文幫我查", _ch(0.95), True),
    ("法文繼續播放啊", _ch(0.3), True),           # 聽錯的喚醒詞照放
    ("你說什麼", _ch(0.3, alt=0.8), True),        # N-best 救援（WakeAltRescue）照放
    ("冰充電並播放啊", None, True),               # legacy 無 fusion 路徑不動
    ("那我把馬文停止播放", _ch(0.3), True),        # 句中真實喚醒詞照放
])
def test_iba_wake_word_ok(text, ch, expected):
    assert _Cog()._iba_wake_word_ok("Alice", text, True, ch) is expected


def test_iba_wake_word_ok_keeps_false():
    assert _Cog()._iba_wake_word_ok("Alice", "馬文下一首", False, _ch(1.0)) is False


def test_wakeless_in_window_counts_mangled_wake_as_named():
    from wake_session import WakeSession
    cog = _Cog()
    cog._wake_session = WakeSession()
    t0 = time.time()
    cog._wake_session.on_wake_accepted("Alice", t0)
    cog._wake_session.on_replied(t0)
    assert cog._wakeless_in_window("Alice", "法文下一首") is False
    assert cog._wakeless_in_window("Alice", "下一首") is True


# ── handle_stt_result 端到端 ────────────────────────────────────────────────

from tests.test_followup_listening import _make_cog_for_stt


@pytest.mark.asyncio
@pytest.mark.parametrize("text,voice,accepted", [
    ("冰充電並播放啊", 0.3, False),
    ("馬文下一首", 1.0, True),
])
async def test_handle_stt_result_requires_wake_word(text, voice, accepted):
    cog, wf = _make_cog_for_stt()
    wf.is_open.return_value = False
    wf.multi_channel_decide.return_value = (True, 0.57, _ch(voice))
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    await cog.handle_stt_result("Alice", text, time.time(), b"fake_audio", bypass_etd=True)
    assert cog._wake_response_pending is accepted


def test_strip_wake_word_mid_sentence_and_mangled():
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    assert vc._strip_wake_word("那我把馬文停止播放") == "停止播放"
    assert vc._strip_wake_word("麻煩下一首") == "下一首"
    assert vc._strip_wake_word("打完播放左邊的人") == "播放左邊的人"
    assert vc._strip_wake_word("拿我下一首") == "下一首"
    assert vc._strip_wake_word("馬文播放晴天") == "播放晴天"

