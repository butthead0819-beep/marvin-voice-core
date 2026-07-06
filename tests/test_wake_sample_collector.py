"""wake_sample_collector.collect 守門測試——只在 env on + owner + raw 含喚醒詞 時存。"""
from pathlib import Path

import pytest

import wake_sample_collector as wsc

_OWNER = 876758076831723580
_OTHER = 111111111111111111


@pytest.fixture
def _wav(tmp_path):
    p = tmp_path / "src.wav"
    p.write_bytes(b"RIFF....fake wav....")
    return str(p)


@pytest.fixture(autouse=True)
def _sample_dir(tmp_path, monkeypatch):
    d = tmp_path / "wake_samples"
    monkeypatch.setattr(wsc, "_DIR", d)
    monkeypatch.setenv("MARVIN_OWNER_ID", str(_OWNER))
    return d


def _saved(d: Path) -> list:
    return sorted(p.name for p in d.glob("*.wav")) if d.exists() else []


def test_env_off_does_not_collect(_wav, _sample_dir, monkeypatch):
    monkeypatch.delenv("MARVIN_COLLECT_WAKE_WAV", raising=False)
    wsc.collect(_wav, _OWNER, "馬文播放周杰倫")
    assert _saved(_sample_dir) == []


def test_non_owner_does_not_collect(_wav, _sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_WAKE_WAV", "1")
    wsc.collect(_wav, _OTHER, "馬文播放周杰倫")
    assert _saved(_sample_dir) == []


def test_no_wake_word_in_text_does_not_collect(_wav, _sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_WAKE_WAV", "1")
    wsc.collect(_wav, _OWNER, "那片板叫什麼ESP32")  # 無喚醒詞
    assert _saved(_sample_dir) == []


def test_missing_wav_does_not_crash(_sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_WAKE_WAV", "1")
    wsc.collect("/nonexistent/x.wav", _OWNER, "馬文你好")
    assert _saved(_sample_dir) == []


def test_owner_wake_env_on_collects_wav_and_sidecar(_wav, _sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_WAKE_WAV", "1")
    wsc.collect(_wav, _OWNER, "馬文播放周杰倫")
    wavs = _saved(_sample_dir)
    assert len(wavs) == 1
    # sidecar json 存 raw_text
    import json
    js = list(_sample_dir.glob("*.json"))
    assert len(js) == 1
    meta = json.loads(js[0].read_text(encoding="utf-8"))
    assert meta["raw"] == "馬文播放周杰倫"
    assert meta["user_id"] == _OWNER
