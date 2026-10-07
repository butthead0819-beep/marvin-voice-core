"""TDD：device/puck_mixer.py::track_position_s + device/avrcp_media_player.py::
build_track_update——兩支純函式（不碰 dbus/time），補 AVRCP mpris:length +
Position 讓 BMW 車機能算剩餘時間，見 device/avrcp_media_player.py 開頭
2026-10-07 說明。"""
from device.avrcp_media_player import build_track_update
from device.puck_mixer import track_position_s


def test_track_position_s_none_start_returns_none():
    assert track_position_s(None, 200.0, now=1000.0) is None


def test_track_position_s_future_start_clamps_to_zero():
    assert track_position_s(song_start_time=1000.0, duration_s=200.0, now=990.0) == 0


def test_track_position_s_elapsed_past_duration_clamps_to_duration():
    assert track_position_s(song_start_time=0.0, duration_s=200.0, now=500.0) == 200.0


def test_track_position_s_no_duration_has_no_cap():
    assert track_position_s(song_start_time=0.0, duration_s=None, now=500.0) == 500.0


def test_build_track_update_includes_length_in_microseconds():
    update = build_track_update("歌名", "歌手", "專輯", duration_s=215.0, position_s=50.0)
    assert update["metadata"]["mpris:length"] == 215_000_000
    assert update["position_us"] == 50_000_000


def test_build_track_update_omits_length_when_duration_missing():
    update = build_track_update("歌名", duration_s=None, position_s=None)
    assert "mpris:length" not in update["metadata"]
    assert update["position_us"] == 0


def test_build_track_update_omits_length_when_duration_zero():
    update = build_track_update("歌名", duration_s=0, position_s=None)
    assert "mpris:length" not in update["metadata"]


def test_build_track_update_metadata_fields():
    update = build_track_update("歌名", "歌手", "專輯")
    assert update["metadata"]["xesam:title"] == "歌名"
    assert update["metadata"]["xesam:artist"] == ["歌手"]
    assert update["metadata"]["xesam:album"] == "專輯"


def test_build_track_update_empty_artist_is_empty_list():
    update = build_track_update("歌名")
    assert update["metadata"]["xesam:artist"] == []
