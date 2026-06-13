"""Sink 串流接線（Volatile Phase 1 hot sprint）。

驗證惰性 session、單一講者佔用、early cut 鏡像 VAD 切句、OFF 時零行為。
不碰真 daemon（注入 fake session）；不碰真 audio thread（直接呼叫 helper）。
"""
from __future__ import annotations

from unittest.mock import MagicMock


def _make_sink():
    from discord_voice_engine import RealtimeVADSink
    cuts = []

    async def on_cut(user_id, audio, ts, **kw):
        cuts.append((user_id, audio, ts))

    sink = RealtimeVADSink(on_speech_cut_callback=on_cut)
    sink.loop = MagicMock()
    # create_task 不真跑，但要關掉傳入的 coroutine 避免 "never awaited" 警告
    def _fake_create_task(coro, *a, **k):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()
    sink.loop.create_task.side_effect = _fake_create_task
    return sink, cuts


class _FakeSession:
    def __init__(self, ready=True):
        self.available = True
        self._ready = ready
        self.begun = []
        self.fed = []
        self.finalized = 0
        self.active_cut = None

    @property
    def ready(self): return self._ready and self.available
    def set_active_cut(self, cb): self.active_cut = cb
    def begin(self, temp=None): self.begun.append(temp)
    def feed(self, pcm): self.fed.append(pcm)
    def finalize(self): self.finalized += 1


# ── OFF：零行為 ─────────────────────────────────────────────────────────────

def test_off_by_default_no_claim(monkeypatch):
    monkeypatch.delenv("STT_STREAMING", raising=False)
    sink, _ = _make_sink()
    sink._stream_maybe_begin(123)
    assert sink._stream_speaker is None
    assert sink._stream_session is None


# ── 暖機未好時走純 VAD（不佔用、不卡使用者）──────────────────────────────────

def test_not_ready_falls_back_to_vad(monkeypatch):
    monkeypatch.setenv("STT_STREAMING", "true")
    sink, _ = _make_sink()
    sink._stream_session = _FakeSession(ready=False)  # 模型還沒暖好
    sink._stream_maybe_begin(123)
    assert sink._stream_speaker is None  # 不佔用，走 VAD


# ── 單一講者佔用 ────────────────────────────────────────────────────────────

def test_single_speaker_claim_and_feed(monkeypatch):
    monkeypatch.setenv("STT_STREAMING", "true")
    sink, _ = _make_sink()
    sink._stream_session = _FakeSession()
    sink.temperature_callback = lambda: 1.5  # → mid

    sink._stream_maybe_begin(123)
    assert sink._stream_speaker == 123
    assert sink._stream_session.begun == ["mid"]
    assert sink._stream_session.active_cut == sink._stream_on_cut  # cut 路由到本 Sink

    # 第二個講者進不來（單一活躍）
    sink._stream_maybe_begin(456)
    assert sink._stream_speaker == 123


def test_feed_downsamples_and_forwards(monkeypatch):
    monkeypatch.setenv("STT_STREAMING", "true")
    import numpy as np
    sink, _ = _make_sink()
    sink._stream_session = _FakeSession()
    # 48k stereo int16：0.1s
    pcm = np.zeros(4800 * 2, dtype=np.int16).tobytes()
    sink._stream_feed(pcm)
    assert len(sink._stream_session.fed) == 1
    # 16k mono：約 1/6 sample 數 × 2 bytes
    assert 0 < len(sink._stream_session.fed[0]) < len(pcm)


def test_temperature_label_mapping(monkeypatch):
    monkeypatch.setenv("STT_STREAMING", "true")
    sink, _ = _make_sink()
    sink._stream_session = _FakeSession()
    for secs, label in [(3.0, "high"), (1.5, "mid"), (0.8, "low")]:
        sink._stream_speaker = None
        sink._stream_session.begun.clear()
        sink.temperature_callback = lambda s=secs: s
        sink._stream_maybe_begin(1)
        assert sink._stream_session.begun == [label]


# ── early cut 鏡像 VAD ──────────────────────────────────────────────────────

def test_on_cut_fires_pipeline_and_releases(monkeypatch):
    monkeypatch.setenv("STT_STREAMING", "true")
    sink, _ = _make_sink()
    sink._stream_session = _FakeSession()
    sink._stream_speaker = 123
    sink.user_buffers[123] = bytearray(b"\x01\x02" * 12000)  # > 19200 bytes
    sink.user_last_spoken_time[123] = 999.0
    sink.user_is_speaking[123] = True

    sink._stream_on_cut("馬文播放晴天", {"source": "semantic_endpoint", "revision_count": 0})

    assert sink._stream_speaker is None              # 釋放
    assert sink.user_buffers[123] == bytearray()     # buffer 已消費
    assert sink.user_last_spoken_time[123] == 0      # 防 VAD 二次切
    assert sink.user_is_speaking[123] is False
    sink.loop.create_task.assert_called()            # pipeline 已觸發


def test_on_cut_ignored_when_buffer_too_short(monkeypatch):
    monkeypatch.setenv("STT_STREAMING", "true")
    sink, _ = _make_sink()
    sink._stream_session = _FakeSession()
    sink._stream_speaker = 123
    sink.user_buffers[123] = bytearray(b"\x01" * 100)  # 雜訊長度

    sink._stream_on_cut("嗯", {"source": "semantic_endpoint", "revision_count": 0})

    assert sink._stream_speaker == 123  # 沒切、沒釋放


def test_release_finalizes_daemon():
    from discord_voice_engine import RealtimeVADSink
    sink = RealtimeVADSink(on_speech_cut_callback=lambda *a, **k: None)
    sink.loop = MagicMock()
    sink._stream_session = _FakeSession()
    sink._stream_speaker = 123
    sink._stream_release(123)
    assert sink._stream_session.finalized == 1
    assert sink._stream_speaker is None
