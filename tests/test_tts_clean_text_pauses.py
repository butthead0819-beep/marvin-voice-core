"""
_clean_text 停頓標點處理（docs/tts_breath_plan.md Phase 1+2）。

全形 。！？ 保留不轉半形（edge 停頓 ≈1.5s vs 半形/逗號 ≈0.4s）；
⋯ / 破折號 / 波浪號先轉成等價標點再走既有清洗路徑；
連續停頓標點（節點號、「？！」、「。，」）收斂成單一 。，避免停頓疊加；
超過 14 字沒標點的長片段（尤其夾《歌名》）會在安全位置補一個逗號換氣。
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
        ("我想想⋯⋯好吧", "我想想。好吧"),
        ("我想想⋯好吧", "我想想。好吧"),
        ("欸——你說什麼", "欸,你說什麼"),
        ("欸—你說什麼", "欸,你說什麼"),
        ("好啦～走吧", "好啦,走吧"),
        ("好啦~~走吧", "好啦,走吧"),
        ("嗯…其實，我不知道。", "嗯。其實,我不知道。"),
        ("這首歌，嗯，還不錯吧？", "這首歌,嗯,還不錯吧？"),
        ("真的嗎？！", "真的嗎？"),
        ("好。。。走吧", "好。走吧"),
        ("好。，走吧", "好。走吧"),
        ("你好嗎?我很好!", "你好嗎？我很好！"),
        ("圓周率是3.14", "圓周率是3.14"),
        ("這首《我愛的人》是挖給showay的新貨", "這首我愛的人,是挖給showay的新貨"),
        ("我剛剛一直在想所以這首歌就留給你", "我剛剛一直在想,所以這首歌就留給你"),
        ("你當時說這旋律太適合一個人發呆", "你當時說這旋律太適合一個人發呆"),
        ("我一直很喜歡《晴天》的那段前奏跟副歌旋律", "我一直很喜歡晴天的那段前奏跟副歌旋律"),
        ("所以我一直覺得這首歌真的很適合現在", "所以我一直覺得這首歌真的很適合現在"),
        ("下一首《夜曲》然後換你", "下一首夜曲然後換你"),
        ("…好吧", "好吧"),
        ("我一直在想這件事所以就先聽吧", "我一直在想這件事所以就先聽吧"),
        ("我一直在想這件事所以就先聽聽吧", "我一直在想這件事,所以就先聽聽吧"),
    ],
)
def test_clean_text_preserves_pause_punctuation(monkeypatch, text, expected):
    tts = _make_tts(monkeypatch)
    assert tts._clean_text(text) == expected
