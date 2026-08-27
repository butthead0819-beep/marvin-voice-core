"""TDD: 點歌佇列排序（2026-06-15 使用者要求）。

規則：
  - Marvin 自動點歌（requested_by 以 'Marvin' 開頭）順位最低，永遠排在所有
    使用者點歌之後。
  - 使用者點歌彼此照**點歌順序**播（FIFO），不再 LIFO（後點先播）。

實作：使用者曲插在「最後一首使用者曲之後、第一首 Marvin 曲之前」的邊界。
不打斷正在播的那首（維持既有 _stream_loop pop(0) 設計）。
"""
from __future__ import annotations

from unittest.mock import MagicMock


def _u(name, vid):
    return {"title": vid, "requested_by": name,
            "webpage_url": f"https://youtu.be/{vid}", "url": "x"}


def _m(vid):
    return {"title": vid, "requested_by": "Marvin推薦（為showay）",
            "webpage_url": f"https://youtu.be/{vid}", "url": "x"}


# ── 純函式：插入位置 ─────────────────────────────────────────────────────────

def _idx(queue):
    from cogs.voice_controller import VoiceController
    return VoiceController._user_song_insert_index(queue)


def test_insert_index_empty_queue():
    assert _idx([]) == 0


def test_insert_index_all_user_songs_goes_to_end():
    """全是使用者曲 → 插最後（FIFO）。"""
    assert _idx([_u("a", "1"), _u("b", "2")]) == 2


def test_insert_index_all_marvin_goes_to_front():
    """全是 Marvin 曲 → 插最前（使用者一律優先）。"""
    assert _idx([_m("1"), _m("2")]) == 0


def test_insert_index_at_user_marvin_boundary():
    """混合 → 插在最後使用者曲之後、第一首 Marvin 之前。"""
    assert _idx([_u("a", "1"), _u("b", "2"), _m("3"), _m("4")]) == 2


# ── 整合：_queue_user_song 連續點兩首 = FIFO，且都在 Marvin 之前 ─────────────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.music_memory = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def test_two_user_songs_play_in_request_order():
    cog = _make_cog()
    cog.stream_queue = [_m("marvin1")]          # 佇列原本只有 Marvin filler
    cog._queue_user_song(_u("jack", "A"))        # 先點 A
    cog._queue_user_song(_u("jack", "B"))        # 後點 B
    titles = [s["title"] for s in cog.stream_queue]
    assert titles == ["A", "B", "marvin1"], f"應 FIFO 且 Marvin 墊底，實際 {titles}"


def test_user_song_jumps_ahead_of_marvin():
    cog = _make_cog()
    cog.stream_queue = [_m("m1"), _m("m2")]
    cog._queue_user_song(_u("jack", "A"))
    assert cog.stream_queue[0]["title"] == "A"


def test_user_song_into_empty_queue():
    cog = _make_cog()
    cog.stream_queue = []
    cog._queue_user_song(_u("jack", "A"))
    assert [s["title"] for s in cog.stream_queue] == ["A"]


def test_user_song_updates_t2_seed():
    """使用者點歌 → _last_user_song_seed 更新成那首 video_id（T2 radio seed 跟著走）。"""
    cog = _make_cog()
    cog.stream_queue = []
    cog._queue_user_song({
        "title": "稻香", "url": "http://x", "requested_by": "alice",
        "webpage_url": "https://www.youtube.com/watch?v=ABCDEFGHIJK",
    })
    assert cog._last_user_song_seed == "ABCDEFGHIJK"


def test_autopilot_user_song_becomes_third_not_next():
    """autopilot 播放中：點歌往後推一首，不插隊到下一首（爆音修 2026-08-27）。

    A 正在播 → queue=[m1]（下一首，歌尾常已預載）。點 X → 應變 [m1, X]，
    播放序 A→m1→X，X 是第三首；不是 [X, m1] 讓 X 搶掉已預載的 m1。
    """
    cog = _make_cog()
    cog.stream_mode = True
    cog.stream_queue = [_m("m1")]
    cog._queue_user_song(_u("jack", "X"))
    assert [s["title"] for s in cog.stream_queue] == ["m1", "X"]


def test_autopilot_user_songs_still_fifo_among_themselves():
    cog = _make_cog()
    cog.stream_mode = True
    cog.stream_queue = [_m("m1")]
    cog._queue_user_song(_u("jack", "A"))
    cog._queue_user_song(_u("jack", "B"))
    assert [s["title"] for s in cog.stream_queue] == ["m1", "A", "B"]


def test_non_autopilot_user_song_still_plays_next():
    """非 autopilot（stream_mode=False）→ 維持舊行為，點歌插到 Marvin 曲之前。"""
    cog = _make_cog()
    cog.stream_mode = False
    cog.stream_queue = [_m("m1"), _m("m2")]
    cog._queue_user_song(_u("jack", "A"))
    assert cog.stream_queue[0]["title"] == "A"


def test_user_song_clears_highlight_start_and_sets_voice_request():
    """使用者點歌不快進：清空 highlight_start_s 並標記 voice_request（避免熱力圖與前奏跳過）。"""
    cog = _make_cog()
    cog.stream_queue = []
    song = {
        "title": "稻香",
        "url": "http://x",
        "requested_by": "alice",
        "webpage_url": "https://www.youtube.com/watch?v=ABCDEFGHIJK",
        "highlight_start_s": 42.5,
    }
    cog._queue_user_song(song)
    queued = cog.stream_queue[0]
    assert queued["highlight_start_s"] is None
    assert queued.get("voice_request") is True

