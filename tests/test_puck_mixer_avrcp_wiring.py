"""car puck mk2 AVRCP metadata 掛勾：PuckMixer 換歌時該不該呼叫 on_track_change，
見 device/avrcp_media_player.py 開頭說明（2026-08-17 Soundcore/BMW 對照測試）。"""
import time
from unittest.mock import MagicMock

from device.puck_mixer import PuckMixer


def test_play_calls_on_track_change_with_title(monkeypatch):
    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=calls.append)
    monkeypatch.setattr("device.puck_mixer.resolve_stream_url", lambda url, timeout=30.0, seek=None: "cdn://x")
    monkeypatch.setattr("device.puck_mixer._make_decoder", lambda url: MagicMock())
    monkeypatch.setattr(mixer, "_ensure_loop_running", lambda: None)

    mixer.play("https://youtube.com/watch?v=abc", title="測試歌")

    assert calls == ["測試歌"]


def test_play_without_title_does_not_call_hook(monkeypatch):
    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=calls.append)
    monkeypatch.setattr("device.puck_mixer.resolve_stream_url", lambda url, timeout=30.0, seek=None: "cdn://x")
    monkeypatch.setattr("device.puck_mixer._make_decoder", lambda url: MagicMock())
    monkeypatch.setattr(mixer, "_ensure_loop_running", lambda: None)

    mixer.play("https://youtube.com/watch?v=abc")

    assert calls == []


def test_no_hook_configured_is_safe(monkeypatch):
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")  # on_track_change 預設 None
    monkeypatch.setattr("device.puck_mixer.resolve_stream_url", lambda url, timeout=30.0, seek=None: "cdn://x")
    monkeypatch.setattr("device.puck_mixer._make_decoder", lambda url: MagicMock())
    monkeypatch.setattr(mixer, "_ensure_loop_running", lambda: None)

    mixer.play("https://youtube.com/watch?v=abc", title="不該炸")  # 不該丟例外


def test_queue_next_stores_title_on_deck(monkeypatch):
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr("device.puck_mixer.resolve_stream_url", lambda url, timeout=30.0, seek=None: "cdn://x")
    monkeypatch.setattr("device.puck_mixer._make_decoder", lambda url: MagicMock(stdout=MagicMock(read=lambda n: b"")))
    monkeypatch.setattr(
        "device.puck_mixer._read_chunk",
        lambda proc: __import__("numpy").zeros(1, dtype="int16"),
    )

    mixer.queue_next("https://youtube.com/watch?v=next", title="下一首")
    # queue_next() 起背景 thread；給它一點時間把 deck_b 裝好（peek 讀取用假資料，很快）。
    for _ in range(50):
        if mixer._deck_b is not None:
            break
        time.sleep(0.02)

    assert mixer._deck_b is not None
    assert mixer._deck_b.get("title") == "下一首"


def test_loop_swap_fires_on_track_change_with_deck_b_title(monkeypatch):
    """crossfade_gains 已到 1.0 時，_loop() 真的跑一輪該把 deck_b 接手成 deck_a
    並觸發 on_track_change(deck_b 的 title)——直接跑真正的 _loop()（monkeypatch
    掉 _open_pcm，開發機沒有 alsaaudio），不是重寫一份等效邏輯。"""
    import numpy as np

    from device.puck_mixer import CHANNELS, CHUNK_FRAMES

    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=calls.append)
    monkeypatch.setattr(mixer, "_open_pcm", lambda: MagicMock())

    zeros = np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)
    monkeypatch.setattr("device.puck_mixer._read_chunk_deck", lambda deck: zeros)

    deck_a = {"url": "a", "proc": MagicMock()}
    deck_b = {"url": "b", "proc": MagicMock(), "title": "接手的歌"}
    mixer._deck_a = deck_a
    mixer._deck_b = deck_b
    mixer._current_url = "a"
    mixer._next_url = "b"
    mixer._crossfade_start = 0.0  # 早就超過 duration，第一輪就會判定 gain_b>=1.0
    mixer._crossfade_duration = 0.001

    import threading
    t = threading.Thread(target=mixer._loop, daemon=True)
    t.start()
    for _ in range(100):
        if calls:
            break
        time.sleep(0.02)
    mixer._stop_flag.set()
    t.join(timeout=1.0)

    assert calls == ["接手的歌"]
    assert mixer._deck_a is deck_b
    assert mixer._deck_b is None


def test_loop_promotes_deck_b_immediately_when_deck_a_hits_real_eof(monkeypatch):
    """2026-08-19 實機修：deck_a 的 eof_event 被設起來（真的播完了，見
    _reader_loop）、deck_b 已經 ready 時，_loop() 該立刻扶正，不用等 Mac
    排定的 crossfade 時間點才動——那個時間點可能還沒到，deck_a 卻已經真的
    沒音訊了。這條沒有設 _crossfade_start（模擬 crossfade 根本還沒被
    Mac 觸發），純粹靠 eof_event 自己接手。"""
    import numpy as np
    import threading

    from device.puck_mixer import CHANNELS, CHUNK_FRAMES

    calls = []
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF", on_track_change=calls.append)
    monkeypatch.setattr(mixer, "_open_pcm", lambda: MagicMock())

    zeros = np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)
    monkeypatch.setattr("device.puck_mixer._read_chunk_deck", lambda deck: zeros)

    eof_event = threading.Event()
    eof_event.set()  # deck_a 已經真的讀到結尾
    deck_a = {"url": "a", "proc": MagicMock(), "eof_event": eof_event}
    deck_b = {"url": "b", "proc": MagicMock(), "title": "接手的歌"}
    mixer._deck_a = deck_a
    mixer._deck_b = deck_b
    mixer._current_url = "a"
    mixer._next_url = "b"
    # 故意不設 _crossfade_start——crossfade() 根本沒被呼叫過，純測 eof_event 自救。

    t = threading.Thread(target=mixer._loop, daemon=True)
    t.start()
    for _ in range(100):
        if calls:
            break
        time.sleep(0.02)
    mixer._stop_flag.set()
    t.join(timeout=1.0)

    assert calls == ["接手的歌"]
    assert mixer._deck_a is deck_b
    assert mixer._deck_b is None
    deck_a["proc"].terminate.assert_called_once()
