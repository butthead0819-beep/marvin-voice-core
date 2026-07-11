"""同歌不同上傳（跨 video-id）重複的 dedup helper 測試。

早上 autopilot 重複嚴重的根因：同一首歌在 YouTube 有多個上傳（官方 MV vs
純歌名版）→ video-id 不同 → video-id dedup 漏；normalize_title 又因藝人前綴/
官方後綴使長短標題不 exact 相等 → title ring 也漏。find_recent_same_song 補這層。
"""
from music_recommender import find_recent_same_song


def test_short_title_matches_recent_long_upload_returns_match():
    # 「你說話的聲音好細」(純歌名) 應命中最近播過的長版官方 MV（同歌不同上傳）
    recent = ["JOYCE 就以斯 - 你說話的聲音好細 (Official Music Video)"]
    assert find_recent_same_song("你說話的聲音好細", recent) == recent[0]


def test_long_upload_matches_recent_short_title_returns_match():
    # 反向：長版官方標題應命中最近播過的純歌名版
    recent = ["你說話的聲音好細"]
    assert (
        find_recent_same_song(
            "JOYCE 就以斯 - 你說話的聲音好細 (Official Music Video)", recent
        )
        == recent[0]
    )


def test_大城小愛_artist_prefixed_variant_deduped():
    recent = ["王力宏 Leehom Wang《大城小愛》(Official Video Karaoke)"]
    assert find_recent_same_song("大城小愛", recent) == recent[0]


def test_same_artist_different_song_not_deduped():
    # 同歌手不同歌不可誤殺（周杰倫 星晴 vs 周杰倫 告白氣球）
    recent = ["周杰倫 Jay Chou【告白氣球 Love Confession】Official MV"]
    assert find_recent_same_song("周杰倫 - 星晴", recent) is None


def test_short_core_substring_not_over_deduped():
    # 「情歌」是「小情歌」的子字串但不同歌；min_core_len 守門防誤殺
    recent = ["蘇打綠 sodagreen - 小情歌 (Live)"]
    assert find_recent_same_song("情歌", recent) is None


def test_exact_normalized_match_deduped():
    recent = ["大城小愛"]
    assert find_recent_same_song("大城小愛 ", recent) == recent[0]


def test_empty_title_or_recent_returns_none():
    assert find_recent_same_song("", ["大城小愛"]) is None
    assert find_recent_same_song("大城小愛", []) is None


def test_no_match_in_recent_returns_none():
    recent = ["五月天 - 傷心的人別聽慢歌", "告五人 - 愛人錯過"]
    assert find_recent_same_song("周杰倫 - 七里香", recent) is None
