"""偵測歌曲『中途被切』：播超過健康門檻(非開頭403)、卻遠短於該首真實總長(<80%)。

背景（2026-07-07 使用者報「播到一半沒指令就跳下一首」）：開頭就 403 的歌 (< MIN_HEALTHY)
走既有 force_fresh 重試路徑；但『播到一半串流 URL 中途失效』的歌 played_s 已很大、被當
正常播完、且 ffmpeg stderr 進 DEVNULL＝log 隱形。這個純函式讓中途切變可見（可診斷）。
"""
from cogs.music_cog import MusicCog

_cut = MusicCog._premature_cut
_MIN = MusicCog._MIN_HEALTHY_PLAY_S


def test_natural_full_play_not_premature():
    assert _cut(128, 124) is False   # 播 128 / 全長 124 = 短 cover 播完
    assert _cut(181, 179) is False


def test_mid_stream_cut_is_premature():
    assert _cut(100, 240) is True    # 播 100 / 全長 240 = 中途切 (~42%)
    assert _cut(120, 300) is True


def test_open_403_not_flagged_here():
    # 開頭就掛(< 健康門檻)歸 403 重試路徑，不算中途切
    assert _cut(1.3, 240) is False


def test_unknown_duration_not_premature():
    assert _cut(100, 0) is False     # 沒總長資訊 → 不誤判
    assert _cut(100, None) is False


def test_near_end_not_premature():
    assert _cut(200, 240) is False   # 播到 83% > 80% 門檻 = 算正常播完
