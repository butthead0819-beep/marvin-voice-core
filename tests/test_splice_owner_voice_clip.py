"""_splice_owner_voice_clip：語音點歌時若能撈到 owner 原音片段，接在 DJ 介紹口白前面；
找不到樣本/非語音點歌/接檔失敗都要原樣退回 dj_audio，不影響既有播放行為。"""
from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest

from cogs.music_cog import MusicCog


def _make_cog():
    return MusicCog.__new__(MusicCog)


def _make_silent_wav(path: Path, duration_s: float = 0.2) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
         "-t", str(duration_s), str(path)],
        capture_output=True, check=True,
    )


pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")


@pytest.mark.asyncio
async def test_no_dj_audio_returns_none():
    cog = _make_cog()
    result = await cog._splice_owner_voice_clip(None, {"voice_request": True})
    assert result is None


@pytest.mark.asyncio
async def test_not_voice_request_returns_dj_audio_unchanged(tmp_path):
    cog = _make_cog()
    dj = tmp_path / "dj.wav"
    _make_silent_wav(dj)
    result = await cog._splice_owner_voice_clip(str(dj), {"voice_request": False})
    assert result == str(dj)


@pytest.mark.asyncio
async def test_no_matching_clip_returns_dj_audio_unchanged(tmp_path, monkeypatch):
    cog = _make_cog()
    dj = tmp_path / "dj.wav"
    _make_silent_wav(dj)
    import owner_song_voice_samples as osvs
    monkeypatch.setattr(osvs, "find_recent_clip", lambda *a, **k: None)
    result = await cog._splice_owner_voice_clip(
        str(dj), {"voice_request": True, "title": "夜曲", "uploader": "周杰倫"}
    )
    assert result == str(dj)


@pytest.mark.asyncio
async def test_match_found_splices_clip_before_dj_audio(tmp_path, monkeypatch):
    cog = _make_cog()
    dj = tmp_path / "dj.wav"
    clip = tmp_path / "clip.wav"
    _make_silent_wav(dj, 0.2)
    _make_silent_wav(clip, 0.3)
    import owner_song_voice_samples as osvs
    monkeypatch.setattr(osvs, "find_recent_clip", lambda *a, **k: str(clip))

    result = await cog._splice_owner_voice_clip(
        str(dj), {"voice_request": True, "title": "夜曲", "uploader": "周杰倫"}
    )

    assert result == f"{dj}.with_clip.wav"
    assert Path(result).exists()
    # 接起來的音檔應該比單獨 dj_audio 長（clip + dj 兩段）
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", result],
        capture_output=True, text=True,
    )
    assert float(probe.stdout.strip()) > 0.4
