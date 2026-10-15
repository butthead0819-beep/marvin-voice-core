"""
_clean_text 停頓標點保留（docs/tts_breath_plan.md Phase 1）。

⋯ / 破折號 / 波浪號原本會被白名單 regex 整個刪掉，停頓感消失；
改為先轉成等價標點（…／，）再走既有清洗路徑。
"""
from __future__ import annotations

import pytest


def _make_tts(monkeypatch, key="fake-key", region="eastasia"):
    if key is None:
        monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    else:
        monkeypatch.setenv("AZURE_SPEECH_KEY", key)
    if region is None:
        monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    else:
        monkeypatch.setenv("AZURE_SPEECH_REGION", region)
    from tts_engine import SukiTTS
    return SukiTTS()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("我想想⋯⋯好吧", "我想想。。。。。。好吧"),
        ("我想想⋯好吧", "我想想。。。好吧"),
        ("欸——你說什麼", "欸,你說什麼"),
        ("欸—你說什麼", "欸,你說什麼"),
        ("好啦～走吧", "好啦,走吧"),
        ("好啦~~走吧", "好啦,走吧"),
        ("嗯…其實，我不知道。", "嗯。。。其實,我不知道."),
        ("這首歌，嗯，還不錯吧？", "這首歌,嗯,還不錯吧?"),
    ],
)
def test_clean_text_preserves_pause_punctuation(monkeypatch, text, expected):
    tts = _make_tts(monkeypatch)
    assert tts._clean_text(text) == expected
