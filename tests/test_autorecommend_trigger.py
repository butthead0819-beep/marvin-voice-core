"""TDD — 佇列空時自動點歌「續推決策」（連續 ambient curation）。

修復根因②：原本 _stream_loop 的 guard `not requested_by.startswith('Marvin')`
讓 Marvin 推薦的歌播完佇列空時不再續推 → 一輪後串流就死。
改成 Marvin 歌也續推（用在場成員當 seed），達成連續 ambient；無人在場才停
（房間空了交給既有 auto-dismiss，不對空房 DJ）。

決策抽成 pure staticmethod _autorecommend_seed 以便獨立測試。
"""
from __future__ import annotations

from cogs.voice_controller import VoiceController


def test_user_song_triggers_with_user_as_seed():
    """使用者點的歌播完佇列空 → 用該使用者當 seed 續推。"""
    assert VoiceController._autorecommend_seed("weakgogo", ["weakgogo", "showay"]) == "weakgogo"


def test_marvin_song_continues_with_online_member_seed():
    """關鍵 regression：Marvin 推薦的歌播完佇列空 → 仍續推（連續 ambient），用在場成員當 seed。"""
    seed = VoiceController._autorecommend_seed("Marvin推薦（為weakgogo）", ["showay", "狗與露"])
    assert seed == "showay"


def test_marvin_song_no_online_members_stops():
    """Marvin 歌播完但房間沒人 → 不續推（不對空房 DJ，交給 auto-dismiss）。"""
    assert VoiceController._autorecommend_seed("Marvin推薦（為weakgogo）", []) is None


def test_unknown_requester_does_not_trigger():
    """'未知' sentinel → 不續推。"""
    assert VoiceController._autorecommend_seed("未知", ["showay"]) is None


def test_empty_requester_does_not_trigger():
    """空 requested_by → 不續推。"""
    assert VoiceController._autorecommend_seed("", ["showay"]) is None
    assert VoiceController._autorecommend_seed(None, ["showay"]) is None
