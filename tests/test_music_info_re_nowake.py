"""TDD：no-wake `_MUSIC_INFO_RE`（IBA Tier 1，voice_controller.py）跟
NowPlayingAgent（wake 路徑）本該「單一語意源」對齊，但實際兩份 regex 分岔了：
NowPlayingAgent 第三組（歌名|歌手|藝人|誰唱|誰寫）trailing 是 `(?:...)?`（optional），
_MUSIC_INFO_RE 少了那個 `?`，導致「誰唱的」「誰寫的」——最口語的問法——wake 路徑接得
到、no-wake 路徑接不到。

真實後果（8/8 討論發現）：使用者說「誰唱的」通常不會先喊喚醒詞，所以實際上大多數
這類提問走的正是 no-wake 這條路；這條路的 regex bug 讓這些提問從沒進過
IntentBus，之前誤判成「使用者根本不太問這個」，其實是「問了但接不到」。
"""
from __future__ import annotations

import pytest

from cogs.voice_controller import _MUSIC_INFO_RE


@pytest.mark.parametrize("query", [
    "誰唱的",
    "誰寫的",
    "這首歌誰唱的",
    "這是誰唱的",
])
def test_bare_who_sings_matches_nowake_path(query):
    assert _MUSIC_INFO_RE.search(query), f"no-wake _MUSIC_INFO_RE 應該接住 {query!r}"


@pytest.mark.parametrize("query", [
    "播放周杰倫", "下一首", "暫停音樂", "誰知道現在幾點",
    "這是誰的東西",  # 「這是誰」不該單獨觸發（跟音樂無關的一般代名詞問句）
])
def test_unrelated_queries_do_not_match(query):
    assert not _MUSIC_INFO_RE.search(query)


# 8/8 使用者回報：實際最常見的兩句是「這是什麼歌？」「這首誰唱的？」——
# 後者已被上面的 `?` 修法接住，前者少了「首」字這條分支，還是漏接。
@pytest.mark.parametrize("query", [
    "這是什麼歌",
    "這是什麼歌？",
    "這什麼歌",
])
def test_this_what_song_matches_nowake_path(query):
    assert _MUSIC_INFO_RE.search(query), f"no-wake _MUSIC_INFO_RE 應該接住 {query!r}"
