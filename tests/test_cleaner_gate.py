"""Shadow pre-gate 量測函式測試（cleaner_gate_decision）。

2026-05-21：先量測「本地 gate 會略過多少 cleaner 呼叫」再決定要不要真接。
gate 邏輯：raw 含喚醒音 OR 音樂詞 OR 對話中 OR Marvin 剛說話 → would_send。
"""
from __future__ import annotations

from stt_cleaner import cleaner_gate_decision


def _send(raw, **kw):
    return cleaner_gate_decision(raw, **kw)[0]


def test_wake_word_sends():
    assert _send("馬文你在嗎") is True
    assert _send("欸 marvin 幫我") is True


def test_phonetic_wake_variant_sends():
    # STT 把「馬文」聽成音近 → gate 仍要送（避免漏接）
    assert _send("麻文播放") is True
    assert _send("媽文") is True


def test_music_keyword_sends_without_wake():
    # 無喚醒詞但有音樂詞（no-wake 直接點歌場景）
    assert _send("播放周杰倫") is True
    assert _send("換一首") is True


def test_conversation_active_sends():
    # 對話進行中（follow-up），即使無喚醒/音樂詞也要送
    assert _send("對啊就是這樣", context_active=True) is True
    assert _send("好", marvin_just_spoke=True) is True


def test_pure_ambient_would_drop():
    # 純環境閒聊：無喚醒、無音樂、非對話中 → gate 會略過
    assert _send("今天天氣真好") is False
    assert _send("你昨天有看球賽嗎") is False


def test_empty_would_drop():
    assert _send("") is False
    assert _send(None) is False


def test_signals_reported():
    _send_flag, sig = cleaner_gate_decision("播放周杰倫", context_active=False, marvin_just_spoke=False)
    assert sig["music"] is True
    assert sig["wake"] is False
    assert sig["ctx"] is False and sig["spoke"] is False
