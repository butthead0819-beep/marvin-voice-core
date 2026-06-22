"""laugh_snapshot.laugh_counts：笑聲當下同時發聲 + 在場人數快照。"""
from types import SimpleNamespace

from laugh_snapshot import laugh_counts


def _member(is_bot):
    return SimpleNamespace(bot=is_bot)


def _vc(members):
    channel = SimpleNamespace(members=members)
    return [SimpleNamespace(channel=channel)]


def test_laugh_counts_counts_recent_voices_and_present():
    now = 1000.0
    sink = SimpleNamespace(user_last_spoken_time={1: 999.0, 2: 998.5, 3: 990.0})
    # 在場 4 人含 1 bot → present=3
    members = [_member(False), _member(False), _member(False), _member(True)]
    voc, present = laugh_counts(sink, _vc(members), now)
    assert voc == 2          # user 1,2 在 3s 內；3 太舊
    assert present == 3      # 排除 bot


def test_laugh_counts_no_sink_no_vc_safe():
    voc, present = laugh_counts(None, [], 1000.0)
    assert (voc, present) == (0, 0)
