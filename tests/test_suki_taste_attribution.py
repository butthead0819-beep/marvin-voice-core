"""記憶(suki likes)影響 autopilot「為誰點歌」：發現/主題新歌沒人點過時，若歌手強匹配某
在場者的 suki 核心愛歌手 → 掛「為X」（強匹配才掛，掛錯名比不掛名傷）。純匹配邏輯測試。
"""
from cogs.music_cog import MusicCog

_match = MusicCog._taste_match_owner


def test_matches_artist_in_suki_likes():
    likes = {"狗與露": ["周杰倫", "MATZKA", "蕭煌奇"], "大肚": ["九零年代金曲", "飲酒聚會"]}
    assert _match("周杰倫 - 稻香 (官方MV)", likes, ["大肚", "狗與露"]) == "狗與露"


def test_no_match_when_artist_not_in_any_likes():
    likes = {"大肚": ["九零年代金曲", "飲酒聚會"]}          # 無具體歌手
    assert _match("周杰倫 - 稻香", likes, ["大肚"]) is None


def test_order_priority_spotlight_first():
    likes = {"A": ["伍佰"], "B": ["伍佰"]}
    assert _match("伍佰 - 挪威的森林", likes, ["B", "A"]) == "B"   # order 前者優先


def test_ignores_non_artist_interests():
    # suki likes 混雜非音樂興趣(露營/股票)不該誤配歌名
    likes = {"陳進文": ["露營", "股票投資", "DIY維修"]}
    assert _match("五月天 - 溫柔", likes, ["陳進文"]) is None


def test_short_like_tokens_skipped():
    likes = {"X": ["宇"]}                                    # 單字太短，防亂配
    assert _match("宇宙人 - 一起去巴黎", likes, ["X"]) is None
