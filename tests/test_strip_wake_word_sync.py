"""_strip_wake_word 與 WAKE_WORDS_LIST 的同步保證（2026-06-13）。

事故：「毛文」補進 WAKE_WORDS_LIST 後喚醒成功，但 voice_controller 的
_WAKE_PATTERNS 是第二份手工同步清單（註解寫「需與 utils.WAKE_WORDS_LIST
同步」但靠人腦），毛文沒同步 → query 沒剝喚醒詞 → 下游把「毛文」當歌名
（Marvin:「我的大腦裡沒有名為毛文的音樂清單」）。

修法：_WAKE_PATTERNS 程式化 = WAKE_WORDS_LIST + FAST_ONLY + 本地額外詞，
本檔的不變量測試讓未來任何 wake 變體新增自動覆蓋剝離路徑。
"""
from __future__ import annotations

from types import SimpleNamespace

from utils import WAKE_WORDS_LIST, FAST_ONLY_WAKE_WORDS


def _get_vc_class():
    from cogs.voice_controller import VoiceController
    return VoiceController


def _strip(text: str) -> str:
    VC = _get_vc_class()
    fake_self = SimpleNamespace(_WAKE_PATTERNS=VC._WAKE_PATTERNS)
    return VC._strip_wake_word(fake_self, text)


def test_all_wake_words_present_in_strip_patterns():
    """不變量：偵測清單的每個詞都必須能被剝離（殺掉雙清單漂移這類 bug）。"""
    VC = _get_vc_class()
    missing = set(WAKE_WORDS_LIST + FAST_ONLY_WAKE_WORDS) - set(VC._WAKE_PATTERNS)
    assert missing == set(), f"WAKE_WORDS_LIST 有但 _WAKE_PATTERNS 沒有: {missing}"


def test_strip_maowen_variant():
    assert _strip("毛文播放想你的夜") == "播放想你的夜"


def test_strip_keeps_local_extras():
    """voice_controller 本地額外詞（龍蝦/媽問等）不能在重構中丟失。"""
    VC = _get_vc_class()
    for extra in ("龍蝦", "媽問", "嗨Mom"):
        assert extra in VC._WAKE_PATTERNS


def test_strip_normal_wake_still_works():
    assert _strip("馬文播放周杰倫的晴天") == "播放周杰倫的晴天"
    assert _strip("嗨馬文你好") == "你好"
