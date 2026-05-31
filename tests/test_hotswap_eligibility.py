"""hotswap_eligibility — 決定一段 TTS 是否短到可走中途熱切換注入（Plan 11 Slice 3）。

中途熱切換成本高（背景起第二條 stream + loudnorm 量測），接縫只在「短句 + 低音量 +
ducking onset」遮掩下可接受（seam test 證實）。所以只有夠短、單行的即時 ack 才走，
其餘維持原本「串流中靜音 / 貼文」。意圖白名單由呼叫端 opt-in，這裡只管長度與 sanity。
"""
from __future__ import annotations

from hotswap_eligibility import MAX_HOTSWAP_CHARS, is_hotswap_eligible


def test_empty_text_not_eligible():
    assert is_hotswap_eligible("") is False
    assert is_hotswap_eligible("   ") is False


def test_short_ack_eligible():
    assert is_hotswap_eligible("好，等一下") is True


def test_over_limit_not_eligible():
    assert is_hotswap_eligible("這是一句很長的回應內容超過字數上限了喔好多字") is False


def test_exactly_at_limit_eligible():
    t = "字" * MAX_HOTSWAP_CHARS
    assert is_hotswap_eligible(t) is True


def test_one_over_limit_not_eligible():
    t = "字" * (MAX_HOTSWAP_CHARS + 1)
    assert is_hotswap_eligible(t) is False


def test_multiline_not_eligible():
    """多行 = 結構化長回應，不走熱切換。"""
    assert is_hotswap_eligible("好\n等一下") is False


def test_strips_whitespace_before_counting():
    """前後空白不算進長度。"""
    padded = "  " + "字" * MAX_HOTSWAP_CHARS + "  "
    assert is_hotswap_eligible(padded) is True


def test_custom_max_chars():
    assert is_hotswap_eligible("好啦好啦", max_chars=3) is False
    assert is_hotswap_eligible("好啦好", max_chars=3) is True


def test_none_text_not_eligible():
    assert is_hotswap_eligible(None) is False
