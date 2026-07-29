"""TDD: BPM 取樣估算 + 落地儲存（純函式部分先行）。

動機：DJ autopilot 選歌目前只有文字層（歌手/主題），沒有節奏資料。與其上 librosa
等重依賴做精準分析，用播放時已在做的取樣基礎設施（見 loudness_norm.py 的
25/50/75% 取樣）順便量 BPM——夠粗但夠用（找「差不多節奏」的鄰近歌，不是要精準
到小數點）。

演算法：onset envelope（frame energy 一階差分，整流）→ autocorrelation 抓週期峰值
→ 換算 BPM。已知限制：倍/半拍 octave error（分不出 120 vs 60/240）——用
_normalize_octave 把估計值摺回常見人聲/流行樂節奏帶（70-180）緩解，不保證消除。
"""
from __future__ import annotations

import numpy as np
import pytest

from bpm_estimate import estimate_bpm_from_pcm, median_bpm, read_bpm_store, write_bpm


def _click_track(bpm: float, sr: int, duration_s: float = 20.0, click_len: int = 30) -> np.ndarray:
    """合成純節拍測試訊號：每拍一個短噪音 click，其餘靜音。"""
    n = int(sr * duration_s)
    pcm = np.zeros(n, dtype=np.float32)
    interval = sr * 60.0 / bpm
    rng = np.random.default_rng(42)
    t = 0.0
    while t < n - click_len:
        i = int(t)
        pcm[i:i + click_len] = rng.uniform(-1, 1, click_len).astype(np.float32)
        t += interval
    return pcm


@pytest.mark.parametrize("bpm", [80.0, 100.0, 128.0, 150.0])
def test_estimate_bpm_from_pcm_recovers_click_track_tempo(bpm):
    sr = 11025
    pcm = _click_track(bpm, sr)
    est = estimate_bpm_from_pcm(pcm, sr)
    assert est is not None
    # octave-tolerant：抓到 bpm、bpm*2、bpm/2 任一都算對（已知限制）
    candidates = [bpm, bpm * 2, bpm / 2]
    assert min(abs(est - c) for c in candidates) < 5.0


def test_estimate_bpm_from_pcm_too_short_returns_none():
    sr = 11025
    pcm = np.zeros(sr // 2, dtype=np.float32)  # 0.5s，不夠估
    assert estimate_bpm_from_pcm(pcm, sr) is None


def test_estimate_bpm_from_pcm_silence_returns_none():
    sr = 11025
    pcm = np.zeros(sr * 20, dtype=np.float32)  # 全靜音，無 onset
    assert estimate_bpm_from_pcm(pcm, sr) is None


def test_median_bpm_filters_none_and_takes_median():
    assert median_bpm([100.0, None, 104.0, 102.0]) == 102.0


def test_median_bpm_all_none_returns_none():
    assert median_bpm([None, None]) is None


def test_median_bpm_empty_returns_none():
    assert median_bpm([]) is None


def test_write_bpm_then_read_roundtrip(tmp_path):
    path = tmp_path / "song_bpm.json"
    write_bpm(str(path), "abc123", 128.0)
    store = read_bpm_store(str(path))
    assert store["abc123"]["bpm"] == 128.0
    assert "ts" in store["abc123"]


def test_write_bpm_merges_existing_entries(tmp_path):
    path = tmp_path / "song_bpm.json"
    write_bpm(str(path), "song_a", 100.0)
    write_bpm(str(path), "song_b", 140.0)
    store = read_bpm_store(str(path))
    assert set(store.keys()) == {"song_a", "song_b"}


def test_read_bpm_store_missing_file_returns_empty(tmp_path):
    path = tmp_path / "nope.json"
    assert read_bpm_store(str(path)) == {}


def test_write_bpm_noop_in_memory_sandbox(tmp_path, monkeypatch):
    import memory_sandbox
    monkeypatch.setattr(memory_sandbox, "active", lambda: True)
    path = tmp_path / "song_bpm.json"
    write_bpm(str(path), "abc123", 128.0)
    assert not path.exists()
