"""免喚醒問歌名：只接短的完整問句（2026-10-24 使用者定）。

指令一律要喚醒詞；問歌名是回答不是指令，且使用者問「誰唱的」通常不先叫名字（8/8 回報），
所以免喚醒保留這一條，但改整句比對：10/17-10/22 三筆誤觸發都是長閒聊句裡碰到
「歌手/歌名」被舊 _MUSIC_INFO_RE 的 search 接走。
"""
from __future__ import annotations

import pytest

from cogs.voice_controller import is_nowake_song_question


@pytest.mark.parametrize("text", [
    "這首是誰唱的", "這首歌誰唱的", "這是誰唱的", "誰唱的", "誰寫的",
    "這是什麼歌", "這是什麼歌？", "這什麼歌", "這首是什麼歌啊",   # 10/18 10/21 10/23 真問
    "這首歌叫什麼", "現在播的是什麼歌", "這 是什麼歌 呀",
])
def test_short_song_question_matches(text):
    assert is_nowake_song_question(text)


@pytest.mark.parametrize("text", [
    "嗯本版中呢聽過等那你綁迴的歌手綁迴歌去的歌手也來了",   # 10/17 誤觸發
    "不過現在臺灣街頭藝人要抽稅",                           # 10/19 誤觸發
    "那講話不是要唱剛好他怎麼，裏麵都是亂搭一些歌名",       # 10/22 誤觸發
    "他找到那一句歌詞啊嗯",
    "這是什麼", "這是誰的東西", "誰知道現在幾點",
    "下一首", "暫停音樂", "播放周杰倫",
    "馬文這是什麼歌",   # 有喚醒詞走 wake 路徑，免喚醒不重派
])
def test_other_text_does_not_match(text):
    assert not is_nowake_song_question(text)
