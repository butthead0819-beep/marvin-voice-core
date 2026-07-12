"""DJ 播報用乾淨歌名抽取（song_name_clean）。

三層優先序：①info['track']（YT Music 結構化）②catalog videoId 精確查
③raw title regex 剝雜訊。只給 DJ 語音路徑用（歌詞路徑仍用 _parse_song_title_artist）。
"""
from song_name_clean import clean_title_regex, dj_display_name


def _vid(url):
    """測試用確定性 video-id 抽取：url 本身即 id。"""
    return url or None


# ── clean_title_regex：純字串清理 ────────────────────────────────────────────
def test_regex_strips_bracket_tag():
    assert clean_title_regex("告白氣球（Official MV）") == "告白氣球"
    assert clean_title_regex("【HD】告白氣球") == "告白氣球"
    assert clean_title_regex("告白氣球 [Official Audio]") == "告白氣球"


def test_regex_strips_separator_tail():
    assert clean_title_regex("告白氣球 | Official Music Video") == "告白氣球"
    assert clean_title_regex("告白氣球 - 高清版") == "告白氣球"


def test_regex_strips_space_separated_cruft_keywords():
    assert clean_title_regex("周杰倫 告白氣球 官方 MV 歌詞版 HD") == "周杰倫 告白氣球"


def test_regex_keeps_song_name_inside_brackets():
    # ⚠️ 中文 YouTube 常把歌名放【】→ 不能無腦剝掉（只剝含 cruft 的括號）
    assert clean_title_regex("周杰倫【告白氣球】Official MV") == "周杰倫 告白氣球"
    assert clean_title_regex("岑寧兒 Yoyo Sham -【追光者】Official Music Video") == "岑寧兒 Yoyo Sham - 追光者"


def test_regex_keeps_clean_title_untouched():
    assert clean_title_regex("周杰倫 告白氣球") == "周杰倫 告白氣球"
    assert clean_title_regex("告白氣球") == "告白氣球"


def test_regex_falls_back_to_raw_when_cleaning_empties():
    # 全是括號雜訊 → 清乾淨會變空 → 回原字串（fail-safe，別回空）
    assert clean_title_regex("【【】】") == "【【】】"


# ── dj_display_name：三層優先序 ──────────────────────────────────────────────
def test_track_field_preferred():
    info = {"track": "告白氣球", "artist": "周杰倫", "title": "【官方MV】告白氣球 歌詞版 HD"}
    assert dj_display_name(info, extract_vid=_vid, catalog_index={}) == ("告白氣球", "周杰倫")


def test_catalog_videoid_hit_returns_clean_name():
    # track 為空、但 videoId 在 catalog → 用結構化 name（已含藝人），artist 回空
    info = {"title": "周杰倫 告白氣球 官方 MV 歌詞版 HD", "url": "RPWDeLqsN0g"}
    idx = {"RPWDeLqsN0g": "周杰倫 告白氣球"}
    assert dj_display_name(info, extract_vid=_vid, catalog_index=idx) == ("周杰倫 告白氣球", "")


def test_regex_fallback_when_no_track_no_catalog():
    info = {"title": "告白氣球（Official MV）", "uploader": "JVR"}
    title, artist = dj_display_name(info, extract_vid=_vid, catalog_index={})
    assert title == "告白氣球"


def test_artist_title_split_preserved():
    # 保留 "Artist - Title" 拆分（先剝雜訊再拆），供 no-track 非 catalog 歌
    info = {"title": "周杰倫 - 告白氣球 (Official MV)"}
    assert dj_display_name(info, extract_vid=_vid, catalog_index={}) == ("告白氣球", "周杰倫")
