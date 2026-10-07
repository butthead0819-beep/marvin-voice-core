"""car puck mk2 AVRCP metadata 掛勾：PuckMixer 該不該呼叫 on_track_change，見
device/avrcp_media_player.py 開頭說明（2026-08-17 Soundcore/BMW 對照測試）。

2026-08-20：換歌決策/DJ口白搬回 Mac 端 mixer，Pi 不再自己知道「現在播哪首」
（見 device/puck_mixer.py 模組說明）——曲名改成背景輪詢 Mac 的 /car_now
（fetch_car_now_track()），變化時才觸發 on_track_change，不是像舊版那樣
play()/queue_next()/crossfade() 換手時同步觸發。

2026-08-21：車機要求同時顯示演出者/專輯——on_track_change 從單一 title 參數
改成 (title, artist, album) 三個位置參數，見 device/avrcp_media_player.py::set_track()。"""
import time

from device.puck_mixer import PuckMixer


def _wait_until(predicate, timeout=1.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_title_poll_calls_on_track_change_when_title_changes(monkeypatch):
    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=lambda *a: calls.append(a))
    monkeypatch.setattr("device.puck_mixer.fetch_car_now_track",
                         lambda: {"title": "測試歌", "artist": "測試歌手", "album": "測試專輯",
                                  "duration_s": None, "song_start_time": None})
    monkeypatch.setattr("device.puck_mixer._TITLE_POLL_INTERVAL_S", 0.01)

    mixer._ensure_title_poll_running()
    assert _wait_until(lambda: calls == [("測試歌", "測試歌手", "測試專輯", None, None)])

    mixer.stop()


def test_title_poll_does_not_refire_when_title_unchanged(monkeypatch):
    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=lambda *a: calls.append(a))
    monkeypatch.setattr("device.puck_mixer.fetch_car_now_track",
                         lambda: {"title": "測試歌", "artist": "", "album": "",
                                  "duration_s": None, "song_start_time": None})
    monkeypatch.setattr("device.puck_mixer._TITLE_POLL_INTERVAL_S", 0.01)

    mixer._ensure_title_poll_running()
    _wait_until(lambda: len(calls) >= 1)
    time.sleep(0.1)   # 讓好幾輪輪詢跑過
    mixer.stop()

    assert calls == [("測試歌", "", "", None, None)]   # 沒有因為輪到同樣的曲名又觸發一次


def test_title_poll_no_hook_configured_is_safe(monkeypatch):
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")  # on_track_change 預設 None
    monkeypatch.setattr("device.puck_mixer.fetch_car_now_track",
                         lambda: {"title": "測試歌", "artist": "", "album": "",
                                  "duration_s": None, "song_start_time": None})
    monkeypatch.setattr("device.puck_mixer._TITLE_POLL_INTERVAL_S", 0.01)

    mixer._ensure_title_poll_running()
    time.sleep(0.05)   # 不該丟例外
    mixer.stop()


def test_title_poll_none_track_does_not_call_hook(monkeypatch):
    """fetch_car_now_track() 回 None（沒在播/連不到 Mac）→ 不觸發、不清掉現有 title。"""
    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=lambda *a: calls.append(a))
    monkeypatch.setattr("device.puck_mixer.fetch_car_now_track", lambda: None)
    monkeypatch.setattr("device.puck_mixer._TITLE_POLL_INTERVAL_S", 0.01)

    mixer._ensure_title_poll_running()
    time.sleep(0.05)
    mixer.stop()

    assert calls == []


def test_title_poll_reports_elapsed_position(monkeypatch):
    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=lambda *a: calls.append(a))
    now = time.time()
    monkeypatch.setattr("device.puck_mixer.fetch_car_now_track",
                         lambda: {"title": "測試歌", "artist": "", "album": "",
                                  "duration_s": 200.0, "song_start_time": now - 50})
    monkeypatch.setattr("device.puck_mixer._TITLE_POLL_INTERVAL_S", 0.01)

    mixer._ensure_title_poll_running()
    assert _wait_until(lambda: len(calls) >= 1)
    time.sleep(0.05)
    mixer.stop()

    assert len(calls) == 1
    title, artist, album, duration_s, position_s = calls[0]
    assert duration_s == 200.0
    assert abs(position_s - 50) <= 2


def test_title_poll_refires_on_same_title_replay(monkeypatch):
    """重播同一首歌：title 不變但 song_start_time 變了，要再觸發一次（見
    device/puck_mixer.py::_title_poll_loop 說明）。"""
    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=lambda *a: calls.append(a))
    track = {"title": "測試歌", "artist": "", "album": "",
             "duration_s": None, "song_start_time": 1000.0}
    monkeypatch.setattr("device.puck_mixer.fetch_car_now_track", lambda: track)
    monkeypatch.setattr("device.puck_mixer._TITLE_POLL_INTERVAL_S", 0.01)

    mixer._ensure_title_poll_running()
    assert _wait_until(lambda: len(calls) >= 1)

    track["song_start_time"] = 2000.0   # 同一首歌重播，start 變了
    assert _wait_until(lambda: len(calls) >= 2)
    mixer.stop()

    assert len(calls) == 2


def test_status_reports_current_title():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    mixer._current_title = "現正播放"
    assert mixer.status()["title"] == "現正播放"
