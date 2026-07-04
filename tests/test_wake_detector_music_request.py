"""TDD: control 頻道補點歌句型（2026-07-04 使用者點題「喚醒流程讓點歌慢」）。

實測：7/3-7/4 點歌 75% 走慢 2 秒的 wakeless 救援路，因為喚醒詞糊掉時
（馬文→把我們，v=0.3）四頻道救不回——control 只認播放控制動詞
（下一首/暫停），「播放X」點歌句拿 0 分，total 0.346 vs 0.35 差 0.004 落榜。

補：句首 6 字內的點歌動詞（容納馬文/把我們/幫我前綴）→ c=0.85。
句中動詞（「他昨天播放了影片」聊天）不中——錨定保精度。
"""
from __future__ import annotations

from wake_detector import _score_control


def test_music_request_at_head_scores():
    assert _score_control("播放黃小琥的沒那麼簡單") >= 0.85


def test_music_request_with_fuzzy_wake_prefix():
    # 7/4 13:37 實案：喚醒詞糊成「把我們」
    assert _score_control("把我們播放黃小虎的沒那麼簡單") >= 0.85


def test_music_request_laiyishou():
    assert _score_control("馬文來一首周杰倫") >= 0.85


def test_chatter_with_midsentence_verb_not_scored():
    # 動詞在句中（>4 字）＝聊天，不得中
    assert _score_control("他昨天在頻道播放了一個影片超好笑") == 0.0


def test_existing_control_patterns_unchanged():
    assert _score_control("下一首") == 0.90
    assert _score_control("暫停音樂") == 0.90


def test_plain_chatter_still_zero():
    assert _score_control("今天天氣不錯欸") == 0.0
