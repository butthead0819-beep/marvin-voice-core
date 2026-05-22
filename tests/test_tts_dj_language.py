"""Regression: DJ 台詞夾英文歌名時 TTS 要走中文語音，不是英文。

Bug root cause: SukiTTS._is_english_text 用 `latin > cjk*2` 比例 → 多字英文歌名
（Shape of You / Never Gonna Give You Up）會把短中文 patter 灌過 2:1 門檻 → 誤走英文語音。
Fix: 只要文字含 ≥1 個中文字就回 False（中文語音）；純英文（零 CJK）才回 True。
"""
from tts_engine import SukiTTS


def _eng(text: str) -> bool:
    return SukiTTS()._is_english_text(text)


# ── 有中文字 → 中文語音（修正前這幾條會誤判英文）──────────────────────────────
def test_dj_line_with_english_song_title_uses_chinese():
    assert _eng("下一首 Shape of You") is False
    assert _eng("播放 Never Gonna Give You Up") is False
    assert _eng("這首歌叫做 Stairway to Heaven 喔") is False
    assert _eng("來首 Bohemian Rhapsody 給還在熬夜的各位") is False


def test_pure_chinese_uses_chinese():
    assert _eng("好的，馬上為你播放") is False
    assert _eng("嘿，這首送給你") is False


# ── 純英文 → 英文語音（保留原行為）─────────────────────────────────────────────
def test_pure_english_stays_english():
    assert _eng("Never Gonna Give You Up") is True
    assert _eng("hello there friend") is True


# ── 邊界：空 / 只有符號數字 → 不是英文（不誤觸英文語音）────────────────────────
def test_empty_returns_false():
    assert _eng("") is False


def test_symbols_and_digits_only_returns_false():
    assert _eng("... 123 !?") is False
