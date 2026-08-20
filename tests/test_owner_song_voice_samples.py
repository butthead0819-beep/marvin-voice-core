"""owner_song_voice_samples 守門測試——只在 env on + owner + 點歌關鍵字 時存；
find_recent_clip 只在字元重疊夠 + 視窗內才配對得到。"""
import json
import time
from pathlib import Path

import pytest

import owner_song_voice_samples as osvs

_OWNER = 876758076831723580
_OTHER = 111111111111111111


@pytest.fixture
def _wav(tmp_path):
    p = tmp_path / "src.wav"
    p.write_bytes(b"RIFF....fake wav....")
    return str(p)


@pytest.fixture(autouse=True)
def _sample_dir(tmp_path, monkeypatch):
    d = tmp_path / "owner_song_voice_samples"
    monkeypatch.setattr(osvs, "_DIR", d)
    monkeypatch.setenv("MARVIN_OWNER_ID", str(_OWNER))
    return d


def _saved(d: Path) -> list:
    return sorted(p.name for p in d.glob("*.wav")) if d.exists() else []


# ── collect 守門 ─────────────────────────────────────────────────────────────

def test_env_off_does_not_collect(_wav, _sample_dir, monkeypatch):
    monkeypatch.delenv("MARVIN_COLLECT_SONG_VOICE_SAMPLES", raising=False)
    osvs.collect(_wav, _OWNER, "馬文播放周杰倫的歌")
    assert _saved(_sample_dir) == []


def test_non_owner_does_not_collect(_wav, _sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_SONG_VOICE_SAMPLES", "1")
    osvs.collect(_wav, _OTHER, "馬文播放周杰倫的歌")
    assert _saved(_sample_dir) == []


def test_non_song_request_text_does_not_collect(_wav, _sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_SONG_VOICE_SAMPLES", "1")
    osvs.collect(_wav, _OWNER, "今天天氣真好")  # 無點歌關鍵字
    assert _saved(_sample_dir) == []


def test_missing_wav_does_not_crash(_sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_SONG_VOICE_SAMPLES", "1")
    osvs.collect("/nonexistent/x.wav", _OWNER, "馬文播放周杰倫的歌")
    assert _saved(_sample_dir) == []


def test_owner_song_request_env_on_collects_wav_and_sidecar(_wav, _sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_SONG_VOICE_SAMPLES", "1")
    osvs.collect(_wav, _OWNER, "馬文播放周杰倫的歌")
    wavs = _saved(_sample_dir)
    assert len(wavs) == 1
    js = list(_sample_dir.glob("*.json"))
    assert len(js) == 1
    meta = json.loads(js[0].read_text(encoding="utf-8"))
    assert meta["raw"] == "馬文播放周杰倫的歌"


# ── find_recent_clip ─────────────────────────────────────────────────────────

def _write_sample(d: Path, stem: str, raw: str, ts: float) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.wav").write_bytes(b"RIFF....fake wav....")
    (d / f"{stem}.json").write_text(json.dumps({"ts": ts, "raw": raw}), encoding="utf-8")


def test_find_recent_clip_no_dir_returns_none(_sample_dir):
    assert osvs.find_recent_clip("周杰倫 夜曲") is None


def test_find_recent_clip_matches_overlapping_text(_sample_dir):
    now = time.time()
    _write_sample(_sample_dir, "a", "馬文播放周杰倫的夜曲", now)
    result = osvs.find_recent_clip("夜曲 周杰倫", max_age_s=1800)
    assert result == str(_sample_dir / "a.wav")


def test_find_recent_clip_expired_returns_none(_sample_dir):
    now = time.time()
    _write_sample(_sample_dir, "a", "馬文播放周杰倫的夜曲", now - 3600)
    assert osvs.find_recent_clip("夜曲 周杰倫", max_age_s=1800) is None


def test_find_recent_clip_low_overlap_returns_none(_sample_dir):
    now = time.time()
    _write_sample(_sample_dir, "a", "今天天氣真好", now)
    assert osvs.find_recent_clip("夜曲 周杰倫", max_age_s=1800) is None


def test_find_recent_clip_picks_most_recent_match(_sample_dir):
    now = time.time()
    _write_sample(_sample_dir, "old", "馬文播放周杰倫的夜曲", now - 100)
    _write_sample(_sample_dir, "new", "馬文播放周杰倫的夜曲", now - 10)
    result = osvs.find_recent_clip("夜曲 周杰倫", max_age_s=1800)
    assert result == str(_sample_dir / "new.wav")


# ── 7 天滾動清除 ─────────────────────────────────────────────────────────────

def test_collect_prunes_expired_samples(_wav, _sample_dir, monkeypatch):
    monkeypatch.setenv("MARVIN_COLLECT_SONG_VOICE_SAMPLES", "1")
    old_ts = time.time() - osvs._TTL_S - 3600
    _write_sample(_sample_dir, "old", "馬文播放舊歌", old_ts)
    import os
    os.utime(_sample_dir / "old.json", (old_ts, old_ts))

    osvs.collect(_wav, _OWNER, "馬文播放周杰倫的歌")

    names = _saved(_sample_dir)
    assert "old.wav" not in names
    assert not (_sample_dir / "old.json").exists()
