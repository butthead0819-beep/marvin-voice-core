"""TDD: 自動點播避開「非單曲」影片（2026-06-15）。

使用者回報 auto 點到古典樂的**合輯/簡介/紀錄片**（大量旁白說話聲），不是歌。
真實兇手（含實測 YouTube 時長）：
  - 'Unveiling Claude Debussy: A Musical Pioneer's Journey'  3:31  ← 旁白紀錄片
  - 'Claude Debussy Full Album'                              74:24 ← 合輯
  - 'The BEST of Debussy, Faure, Ravel, Satie, ...'         426:53 ← 7hr 合輯
  - 'Classical Music for Relaxation #1'                      65:22 ← 合輯
  - 'Debussy - The Girl with the Flaxen Hair (1 hour loop)'  61:43 ← 循環

雙信號避開：①時長 > 15 分鐘（抓 4 個長合輯/loop）②標題黑名單（抓那部
**3:31 的紀錄片**——時長閘漏，只能靠標題）。
"""
from __future__ import annotations

import pytest

from track_quality import is_non_song_video


# ── 必須擋（reject）─────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,dur", [
    ("Unveiling Claude Debussy: A Musical Pioneer's Journey", 211),   # 紀錄片：短，靠標題
    ("Claude Debussy Full Album", 4464),
    ("The BEST of Debussy, Faure, Ravel, Satie, Offenbach, Franck, and Bizet", 25613),
    ("Classical Music for Relaxation #1 | Chopin, Debussy and more", 3922),
    ("Debussy - The Girl with the Flaxen Hair (1 hour loop)", 3703),
])
def test_non_song_videos_rejected(title, dur):
    rejected, _reason = is_non_song_video(title, dur)
    assert rejected is True, f"應被擋: {title!r}"


def test_documentary_rejected_by_title_despite_short_duration():
    """3:31 的紀錄片時長正常，必須靠標題擋下（時長閘漏網的關鍵 case）。"""
    rejected, reason = is_non_song_video(
        "Unveiling Claude Debussy: A Musical Pioneer's Journey", 211)
    assert rejected is True
    assert "title" in reason


def test_long_compilation_rejected_by_duration():
    rejected, reason = is_non_song_video("某某鋼琴精選", 4000)
    assert rejected is True
    assert "duration" in reason


# ── 必須放行（keep，正常單曲）────────────────────────────────────────────────

@pytest.mark.parametrize("title,dur", [
    ("Beethoven - Moonlight Sonata 1st Movement", 373),
    ("Claude Debussy Clair de Lune", 282),
    ("周杰倫 Jay Chou【退後 A Step Back】-Official Music Video", 240),
    ("The Eagles - Hotel California 1976 (Live) - Remaster", 390),
    ("周杰倫 - 告白氣球 (DJ Remix)", 200),   # 'Remix' 含 'mix' 不可誤擋
    ("The Best of Me", 210),                  # 真歌名含 'best of'：靠時長放行、標題不擋
])
def test_real_songs_kept(title, dur):
    rejected, reason = is_non_song_video(title, dur)
    assert rejected is False, f"不該擋: {title!r}（reason={reason}）"


def test_missing_duration_does_not_crash():
    """duration 缺失（None/0）→ 只看標題、不報錯。"""
    assert is_non_song_video("某首歌", None)[0] is False
    assert is_non_song_video("Claude Debussy Full Album", None)[0] is True   # 靠標題
