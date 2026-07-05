"""MusicCog._should_retry_failed_song — 點的歌 403/失敗時是否重抓網址重試的守門。

純函式（classmethod），不需實例化。守門要能區分「403 失敗」vs「使用者 skip」vs
「本來就短的歌」，避免誤重播被 skip 的歌或無限重試自動推薦。
"""
from cogs.music_cog import MusicCog

_R = MusicCog._should_retry_failed_song
_SHORT = MusicCog._MIN_HEALTHY_PLAY_S - 0.1
_LONG = MusicCog._MIN_HEALTHY_PLAY_S + 100


def _base(**over):
    kw = dict(played_s=_SHORT, stream_active=True, skipped=False,
              requested_by="User_local", already_retried=False)
    kw.update(over)
    return _R(kw.pop("played_s"), **kw)


def test_short_playback_user_song_retries():
    """真人點的歌播太短(疑 403)+ 仍串流 + 沒 skip + 沒重試過 → 重試。"""
    assert _base() is True


def test_user_skip_does_not_retry():
    """使用者 skip → 不重試（否則會誤重播 skip 掉的歌）。"""
    assert _base(skipped=True) is False


def test_already_retried_does_not_retry_again():
    """已重試過一次 → 不再重試（防無限迴圈）。"""
    assert _base(already_retried=True) is False


def test_stopped_stream_does_not_retry():
    """stop 指令（stream_active=False）→ 不重試。"""
    assert _base(stream_active=False) is False


def test_healthy_playback_does_not_retry():
    """正常播完（時長 ≥ 健康門檻）→ 非失敗，不重試。"""
    assert _base(played_s=_LONG) is False


def test_marvin_auto_song_does_not_retry():
    """Marvin 自動推薦的歌失敗 → 不重試（只救真人點的歌，自動推薦讓它換下一首）。"""
    assert _base(requested_by="Marvin推薦（點給大家）") is False


def test_no_requester_does_not_retry():
    assert _base(requested_by=None) is False
    assert _base(requested_by="") is False
