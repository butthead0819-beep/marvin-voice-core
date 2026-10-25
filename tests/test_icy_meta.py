"""tests/test_icy_meta.py — marvin_voice_core/icy_meta.py 的純函式/純類別測試。

驗：
(1) truncate_utf8：中文不切半字、ASCII 不超過原樣
(2) build_now_playing_payload：欄位數/順序、清掉控制字元、dur/pos 換算、pos 上下限、
    None info → state S、track_id 隨 start_time 換歌而變
(3) IcyInterleaver：用一個獨立的 ICY 解析函式（規格的解碼方，不複製被測的編碼邏輯）
    驗證音訊還原、完整/空區塊的送出時機
"""
from __future__ import annotations

from marvin_voice_core.icy_meta import (
    ALBUM_MAX_BYTES,
    ARTIST_MAX_BYTES,
    TITLE_MAX_BYTES,
    IcyInterleaver,
    SpeakTracker,
    build_now_playing_payload,
    display_album,
    truncate_utf8,
)


# ---------- truncate_utf8 ----------

def test_truncate_utf8_chinese_does_not_split_char():
    s = "歌" * 41  # 每個「歌」3 bytes，41*3=123 > 120
    out = truncate_utf8(s, 120)
    assert len(out.encode("utf-8")) <= 120
    # 截斷必須落在字元邊界：120/3=40 整，應該剛好拿到 40 個字
    assert out == "歌" * 40


def test_truncate_utf8_ascii_not_over_original():
    s = "hello world"
    assert truncate_utf8(s, 120) == s


def test_truncate_utf8_ascii_cuts_to_exact_byte_len():
    s = "a" * 10
    assert truncate_utf8(s, 5) == "a" * 5


# ---------- build_now_playing_payload ----------

def test_payload_none_info_has_8_fields_and_state_s():
    payload = build_now_playing_payload(None, None, 100.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 8
    assert parts[0] == "M1"
    assert parts[1] == ""
    assert parts[2] == "0"
    assert parts[3] == "0"
    assert parts[4] == "S"
    assert parts[5] == parts[6] == parts[7] == ""


def test_payload_field_order_and_count():
    info = {"title": "Song", "artist": "Artist", "album": "Album", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1050.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 8
    assert parts[0] == "M1"
    assert parts[4] == "P"
    assert parts[5] == "Song"
    assert parts[6] == "Artist"
    assert parts[7] == "Album"


def test_payload_title_control_chars_cleaned():
    info = {"title": "Bad\x1fTitle\nHere\r\x00", "duration": 100.0}
    payload = build_now_playing_payload(info, None, 0.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 8
    assert "\n" not in parts[5] and "\r" not in parts[5] and "\x00" not in parts[5]


def test_payload_dur_pos_conversion():
    info = {"title": "T", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1010.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert parts[2] == "200000"
    assert parts[3] == "10000"


def test_payload_pos_clamped_to_duration():
    info = {"title": "T", "duration": 5.0}
    payload = build_now_playing_payload(info, 1000.0, 1999.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert parts[3] == "5000"


def test_payload_pos_clamped_to_zero_lower_bound():
    info = {"title": "T", "duration": 5.0}
    payload = build_now_playing_payload(info, 1000.0, 990.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert parts[3] == "0"


def test_payload_pos_zero_when_no_start_time():
    info = {"title": "T", "duration": 5.0}
    payload = build_now_playing_payload(info, None, 1999.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert parts[3] == "0"


def test_payload_missing_duration_is_zero():
    info = {"title": "T"}
    payload = build_now_playing_payload(info, 1000.0, 1010.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert parts[2] == "0"


def test_payload_track_id_changes_with_start_time():
    info = {"title": "Same Title"}
    p1 = build_now_playing_payload(info, 1000.0, 1000.0)
    p2 = build_now_playing_payload(info, 2000.0, 2000.0)
    id1 = p1.decode("utf-8").split("\x1f")[1]
    id2 = p2.decode("utf-8").split("\x1f")[1]
    assert id1 != id2


def test_payload_track_id_same_for_same_track():
    info = {"title": "Same Title"}
    p1 = build_now_playing_payload(info, 1000.0, 1000.0)
    p2 = build_now_playing_payload(info, 1000.0, 1050.0)
    id1 = p1.decode("utf-8").split("\x1f")[1]
    id2 = p2.decode("utf-8").split("\x1f")[1]
    assert id1 == id2


def test_payload_fields_truncated_to_max_bytes():
    info = {"title": "A" * 300, "artist": "B" * 300, "album": "C" * 300}
    payload = build_now_playing_payload(info, 1.0, 1.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts[5].encode("utf-8")) <= TITLE_MAX_BYTES
    assert len(parts[6].encode("utf-8")) <= ARTIST_MAX_BYTES
    assert len(parts[7].encode("utf-8")) <= ALBUM_MAX_BYTES


# ---------- IcyInterleaver：獨立 ICY 解析（規格解碼方，不複製被測編碼邏輯） ----------

def _icy_decode(stream: bytes, metaint: int):
    """依 ICY 規格把音訊 bytes 跟 metadata 區塊分開。回傳 (audio_bytes, [meta_payloads])。

    規格：每 metaint bytes 音訊後跟一個長度 byte L，L*16 bytes 的 metadata（含補零），
    L=0 代表沒有 metadata（空區塊）。
    """
    audio = bytearray()
    metas = []
    i = 0
    n = len(stream)
    while i < n:
        take = min(metaint, n - i)
        audio += stream[i:i + take]
        i += take
        if i >= n:
            break
        length_byte = stream[i]
        i += 1
        if length_byte > 0:
            block = stream[i:i + length_byte * 16]
            i += length_byte * 16
            metas.append(block.rstrip(b"\x00"))
    return bytes(audio), metas


def _feed_in_pieces(interleaver, audio: bytes, payload: bytes, now: float, piece_sizes):
    out = bytearray()
    offset = 0
    for size in piece_sizes:
        chunk = audio[offset:offset + size]
        if not chunk:
            break
        out += interleaver.feed(chunk, payload, now)
        offset += size
    if offset < len(audio):
        out += interleaver.feed(audio[offset:], payload, now)
    return bytes(out)


def test_interleaver_audio_bytes_round_trip_arbitrary_chunking():
    metaint = 10
    audio = bytes(range(256)) * 2  # 512 bytes，多個 metaint 邊界
    interleaver = IcyInterleaver(metaint=metaint, full_refresh_s=5.0)
    payload = b"M1\x1f1\x1f1000\x1f0\x1fP\x1ftitle\x1fartist\x1falbum"
    # chunk 大小包含 1 byte chunk 跟跨多個邊界的大 chunk
    piece_sizes = [1, 1, 1, 7, 33, 100, 50]
    out = _feed_in_pieces(interleaver, audio, payload, 0.0, piece_sizes)
    decoded_audio, _ = _icy_decode(out, metaint)
    assert decoded_audio == audio


def test_interleaver_first_block_is_full_payload():
    metaint = 10
    audio = b"x" * 10
    interleaver = IcyInterleaver(metaint=metaint, full_refresh_s=5.0)
    payload = b"M1\x1f1\x1f1000\x1f0\x1fP\x1ftitle\x1fartist\x1falbum"
    out = interleaver.feed(audio, payload, 0.0)
    _, metas = _icy_decode(out, metaint)
    assert len(metas) == 1
    assert metas[0] == payload


def test_interleaver_pos_only_change_within_refresh_window_is_empty():
    metaint = 10
    interleaver = IcyInterleaver(metaint=metaint, full_refresh_s=5.0)
    payload1 = b"M1\x1f1\x1f1000\x1f0\x1fP\x1ftitle\x1fartist\x1falbum"
    payload2 = b"M1\x1f1\x1f1000\x1f500\x1fP\x1ftitle\x1fartist\x1falbum"  # 只有 pos 不同
    out1 = interleaver.feed(b"x" * 10, payload1, 0.0)
    out2 = interleaver.feed(b"x" * 10, payload2, 1.0)  # 1 秒後，未滿 5 秒
    _, metas1 = _icy_decode(out1, metaint)
    _, metas2 = _icy_decode(out2, metaint)
    assert metas1 == [payload1]
    assert metas2 == []  # 空區塊，不是完整 payload


def test_interleaver_title_change_sends_full_immediately():
    metaint = 10
    interleaver = IcyInterleaver(metaint=metaint, full_refresh_s=5.0)
    payload1 = b"M1\x1f1\x1f1000\x1f0\x1fP\x1ftitle-A\x1fartist\x1falbum"
    payload2 = b"M1\x1f2\x1f1000\x1f0\x1fP\x1ftitle-B\x1fartist\x1falbum"
    interleaver.feed(b"x" * 10, payload1, 0.0)
    out2 = interleaver.feed(b"x" * 10, payload2, 0.1)  # 遠小於 5 秒，但內容變了
    _, metas2 = _icy_decode(out2, metaint)
    assert metas2 == [payload2]


def test_interleaver_full_refresh_after_5s_even_if_unchanged():
    metaint = 10
    interleaver = IcyInterleaver(metaint=metaint, full_refresh_s=5.0)
    payload = b"M1\x1f1\x1f1000\x1f0\x1fP\x1ftitle\x1fartist\x1falbum"
    interleaver.feed(b"x" * 10, payload, 0.0)
    out2 = interleaver.feed(b"x" * 10, payload, 5.5)
    _, metas2 = _icy_decode(out2, metaint)
    assert metas2 == [payload]


# ---------- build_now_playing_payload：include_bpm ----------

def test_payload_default_still_8_fields_unchanged():
    info = {"title": "Song", "artist": "Artist", "album": "Album", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1050.0)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 8
    assert parts == ["M1", parts[1], "200000", "50000", "P", "Song", "Artist", "Album"]


def test_payload_include_bpm_rounds():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1050.0, bpm=127.6, include_bpm=True)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 9
    assert parts[8] == "128"


def test_payload_include_bpm_none_is_empty():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1050.0, bpm=None, include_bpm=True)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 9
    assert parts[8] == ""


def test_payload_include_bpm_zero_is_empty():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1050.0, bpm=0, include_bpm=True)
    parts = payload.decode("utf-8").split("\x1f")
    assert parts[8] == ""


def test_payload_include_bpm_negative_is_empty():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1050.0, bpm=-5, include_bpm=True)
    parts = payload.decode("utf-8").split("\x1f")
    assert parts[8] == ""


def test_payload_include_bpm_non_numeric_is_empty():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1050.0, bpm="abc", include_bpm=True)
    parts = payload.decode("utf-8").split("\x1f")
    assert parts[8] == ""


def test_payload_none_info_include_bpm_still_9_fields():
    payload = build_now_playing_payload(None, None, 100.0, bpm=140, include_bpm=True)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 9
    assert parts[4] == "S"
    assert parts[8] == ""


# ---------- IcyInterleaver：9 欄 payload 只有 pos 變動時仍送空區塊 ----------

def test_interleaver_9_field_payload_pos_only_change_is_empty():
    metaint = 10
    interleaver = IcyInterleaver(metaint=metaint, full_refresh_s=5.0)
    payload1 = b"M1\x1f1\x1f1000\x1f0\x1fP\x1ftitle\x1fartist\x1falbum\x1f128"
    payload2 = b"M1\x1f1\x1f1000\x1f500\x1fP\x1ftitle\x1fartist\x1falbum\x1f128"  # 只有 pos 不同
    out1 = interleaver.feed(b"x" * 10, payload1, 0.0)
    out2 = interleaver.feed(b"x" * 10, payload2, 1.0)  # 1 秒後，未滿 5 秒
    _, metas1 = _icy_decode(out1, metaint)
    _, metas2 = _icy_decode(out2, metaint)
    assert metas1 == [payload1]
    assert metas2 == []


# ---------- display_title / display_album：乾淨曲名 + 點播掛名（透過 payload 驗） ----------

def _title_album(info):
    payload = build_now_playing_payload(info, 1000.0, 1000.0)
    parts = payload.decode("utf-8").split("\x1f")
    return parts[5], parts[7]


def test_display_title_prefers_spotify_title_when_contained():
    info = {"title": "周杰倫 Jay Chou【龍捲風 Tornado】-Official Music Video", "spotify_title": "龍捲風"}
    title, _ = _title_album(info)
    assert title == "龍捲風"


def test_display_title_falls_back_to_itunes_title_when_contained():
    info = {"title": "倪安東 Anthony Neely [散場的擁抱 The Last Embrace] Official MV", "itunes_title": "散場的擁抱"}
    title, _ = _title_album(info)
    assert title == "散場的擁抱"


def test_display_title_spotify_wins_over_itunes_when_both_contained():
    info = {"title": "A - 晴天 Official", "spotify_title": "晴天 Official", "itunes_title": "晴天"}
    title, _ = _title_album(info)
    assert title == "晴天 Official"


def test_display_title_mismatch_guard_falls_back_to_yt_title():
    info = {"title": "周杰倫 Jay Chou【珊瑚海 Coral Sea】", "itunes_title": "稻香"}
    title, _ = _title_album(info)
    assert title == "周杰倫 Jay Chou【珊瑚海 Coral Sea】"


def test_display_title_spotify_not_contained_falls_to_itunes():
    info = {"title": "X【後來】MV", "spotify_title": "陰天", "itunes_title": "後來"}
    title, _ = _title_album(info)
    assert title == "後來"


def test_display_title_match_ignores_punctuation_whitespace_case():
    info = {"title": "BOL4 - HUG (Lyrics)", "itunes_title": "Hug"}
    title, _ = _title_album(info)
    assert title == "Hug"


def test_track_id_still_based_on_yt_title_regardless_of_itunes_title():
    base_info = {"title": "周杰倫 Jay Chou【龍捲風 Tornado】-Official Music Video"}
    info_with_itunes = dict(base_info, itunes_title="龍捲風")
    p1 = build_now_playing_payload(base_info, 1000.0, 1000.0)
    p2 = build_now_playing_payload(info_with_itunes, 1000.0, 1000.0)
    id1 = p1.decode("utf-8").split("\x1f")[1]
    id2 = p2.decode("utf-8").split("\x1f")[1]
    assert id1 == id2


def test_display_album_uses_album_when_present():
    info = {"title": "T", "album": "Fantasy"}
    _, album = _title_album(info)
    assert album == "Fantasy"


def test_display_album_falls_back_to_requested_by_name():
    info = {"title": "T", "requested_by": "狗與露"}
    _, album = _title_album(info)
    assert album == "狗與露點播"


def test_display_album_recommend_attribution_kept_as_is():
    info = {"title": "T", "requested_by": "Marvin推薦（為狗與露）"}
    _, album = _title_album(info)
    assert album == "Marvin推薦（為狗與露）"


def test_display_album_generic_recommend_kept_as_is():
    info = {"title": "T", "requested_by": "Marvin推薦（點給大家）"}
    _, album = _title_album(info)
    assert album == "Marvin推薦（點給大家）"


def test_display_album_empty_when_requested_by_missing_or_unknown():
    assert display_album({"title": "T"}) == ""
    assert display_album({"title": "T", "requested_by": "未知"}) == ""


# ---------- SpeakTracker ----------

def test_speak_tracker_first_update_returns_false():
    t = SpeakTracker()
    assert t.update(0, 100.0) is False


def test_speak_tracker_increase_returns_true():
    t = SpeakTracker()
    t.update(5, 100.0)
    assert t.update(6, 100.1) is True


def test_speak_tracker_true_within_hold_false_after():
    t = SpeakTracker(hold_s=1.0)
    t.update(5, 100.0)
    t.update(6, 100.0)  # 增加發生在 100.0
    assert t.update(6, 100.9) is True   # 0.9s 後仍在 hold 內
    assert t.update(6, 101.1) is False  # 1.1s 後超過 hold


def test_speak_tracker_unchanged_count_stays_false():
    t = SpeakTracker(hold_s=1.0)
    t.update(5, 100.0)
    t.update(5, 100.0)  # 沒增加
    assert t.update(5, 100.1) is False


def test_speak_tracker_count_decrease_is_not_increase():
    t = SpeakTracker(hold_s=1.0)
    t.update(10, 100.0)
    t.update(11, 100.0)  # 增加，講話中
    assert t.update(2, 100.1) is False  # 計數變小（mixer 重建）→ 新基準，不算增加
    assert t.update(2, 100.2) is False  # 持平也不算增加


# ---------- build_now_playing_payload：speaking ----------

def test_payload_speaking_none_matches_no_kwarg_with_info():
    info = {"title": "Song", "artist": "Artist", "album": "Album", "duration": 200.0}
    p1 = build_now_playing_payload(info, 1000.0, 1050.0)
    p2 = build_now_playing_payload(info, 1000.0, 1050.0, speaking=None)
    assert p1 == p2


def test_payload_speaking_none_matches_no_kwarg_no_info():
    p1 = build_now_playing_payload(None, None, 100.0)
    p2 = build_now_playing_payload(None, None, 100.0, speaking=None)
    assert p1 == p2


def test_payload_speaking_true_with_info_is_10_fields():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(info, 1000.0, 1050.0, speaking=True)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 10
    assert parts[9] == "1"
    assert parts[8] == ""


def test_payload_speaking_false_with_bpm_is_10_fields():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=False, include_bpm=True, bpm=120)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 10
    assert parts[8] == "120"
    assert parts[9] == "0"


def test_payload_speaking_true_no_info_is_10_fields_state_s():
    payload = build_now_playing_payload(None, None, 100.0, speaking=True)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 10
    assert parts[4] == "S"
    assert parts[9] == "1"


def test_payload_first_8_fields_unchanged_by_speaking():
    info = {"title": "Song", "artist": "Artist", "album": "Album", "duration": 200.0}
    without = build_now_playing_payload(info, 1000.0, 1050.0)
    with_speak = build_now_playing_payload(info, 1000.0, 1050.0, speaking=True)
    parts_without = without.decode("utf-8").split("\x1f")
    parts_with = with_speak.decode("utf-8").split("\x1f")
    assert parts_without[:8] == parts_with[:8]


# ---------- build_now_playing_payload：gesture ----------

def test_payload_speaking_and_gesture_is_11_fields_gesture_verbatim():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=True, gesture="12:Y2")
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 11
    assert parts[10] == "12:Y2"


def test_payload_gesture_ignored_when_speaking_none():
    info = {"title": "Song", "duration": 200.0}
    without = build_now_playing_payload(info, 1000.0, 1050.0)
    with_gesture = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=None, gesture="12:Y2")
    assert without == with_gesture


def test_payload_speaking_with_gesture_none_stays_10_fields():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=True, gesture=None)
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 10


# ---------- build_now_playing_payload：skin ----------

def test_payload_speaking_and_skin_gesture_none_is_12_fields_empty_gesture():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=True, gesture=None, skin="a1b2c3d4")
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 12
    assert parts[10] == ""
    assert parts[11] == "a1b2c3d4"


def test_payload_speaking_and_skin_with_gesture_is_12_fields():
    info = {"title": "Song", "duration": 200.0}
    payload = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=True, gesture="12:Y2", skin="a1b2c3d4")
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 12
    assert parts[10] == "12:Y2"
    assert parts[11] == "a1b2c3d4"


def test_payload_skin_none_matches_output_without_skin_kwarg():
    info = {"title": "Song", "duration": 200.0}
    without = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=True, gesture="12:Y2")
    with_skin_none = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=True, gesture="12:Y2", skin=None)
    assert without == with_skin_none


def test_payload_skin_ignored_when_speaking_none():
    info = {"title": "Song", "duration": 200.0}
    without = build_now_playing_payload(info, 1000.0, 1050.0)
    with_skin = build_now_playing_payload(
        info, 1000.0, 1050.0, speaking=None, skin="a1b2c3d4")
    assert without == with_skin


def test_payload_none_info_speaking_and_skin_is_12_fields():
    payload = build_now_playing_payload(
        None, None, 100.0, speaking=False, gesture=None, skin="a1b2c3d4")
    parts = payload.decode("utf-8").split("\x1f")
    assert len(parts) == 12
    assert parts[4] == "S"
    assert parts[10] == ""
    assert parts[11] == "a1b2c3d4"
