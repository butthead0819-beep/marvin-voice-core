"""TDD: 每段 DJ 串場寫一筆結構化紀錄供一週觀察用 (9/30 使用者定)。

不驗算 clean_dj_script / select_narration_mode 等既有邏輯本身，只驗證
log_dj_narration / probe_audio_seconds 的行為，以及 _fetch_dj_interjection_raw
有把正確的欄位餵進去記錄。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import dj_narration_log
from tests.test_dj_story_context import _exclude, _info, _make_cog


@pytest.mark.asyncio
async def test_log_dj_narration_appends_two_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(dj_narration_log, "_LOG_PATH", tmp_path / "dj_narration.jsonl")
    dj_narration_log.log_dj_narration({"song": "夜曲", "text": "中文測試"})
    dj_narration_log.log_dj_narration({"song": "普通朋友", "text": "第二筆"})

    lines = (tmp_path / "dj_narration.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    entry1 = json.loads(lines[0])
    assert entry1["song"] == "夜曲"
    assert entry1["text"] == "中文測試"
    assert "ts" in entry1

    entry2 = json.loads(lines[1])
    assert entry2["song"] == "普通朋友"

    # 中文未被 escape 成 \uXXXX
    assert "中文測試" in lines[0]


@pytest.mark.asyncio
async def test_log_dj_narration_silent_on_write_failure(tmp_path, monkeypatch):
    blocking_file = tmp_path / "not_a_dir"
    blocking_file.write_text("x")
    monkeypatch.setattr(dj_narration_log, "_LOG_PATH", blocking_file / "dj_narration.jsonl")

    # 不該拋例外
    dj_narration_log.log_dj_narration({"song": "夜曲"})


@pytest.mark.asyncio
async def test_probe_audio_seconds_none_path():
    assert await dj_narration_log.probe_audio_seconds(None) is None
    assert await dj_narration_log.probe_audio_seconds("/no/such/file.opus") is None


@pytest.mark.asyncio
async def test_probe_audio_seconds_parses_ffprobe_output(monkeypatch, tmp_path):
    fake_path = tmp_path / "audio.opus"
    fake_path.write_bytes(b"x")

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"12.345\n", b""))
    fake_proc.kill = MagicMock()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await dj_narration_log.probe_audio_seconds(str(fake_path))
    assert result == 12.35


@pytest.mark.asyncio
async def test_probe_audio_seconds_timeout_kills_proc(monkeypatch, tmp_path):
    fake_path = tmp_path / "audio.opus"
    fake_path.write_bytes(b"x")

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    fake_proc.kill = MagicMock()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await dj_narration_log.probe_audio_seconds(str(fake_path), timeout_s=0.01)
    assert result is None
    fake_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_dj_interjection_logs_llm_path(monkeypatch, tmp_path):
    _exclude(monkeypatch, "quick")
    cog = _make_cog(tmp_path=tmp_path)

    records = []
    monkeypatch.setattr(
        "dj_narration_log.log_dj_narration", lambda record: records.append(record)
    )
    monkeypatch.setattr(
        "dj_narration_log.probe_audio_seconds", AsyncMock(return_value=9.5)
    )

    result = await cog._fetch_dj_interjection_raw(_info())

    assert len(records) == 1
    rec = records[0]
    assert rec["source"] == "llm"
    assert rec["llm_raw"] == "這首夜曲接得剛好，一樣是心事重重的深夜"
    assert rec["text"] == result["text"]
    assert "歌曲：" in rec["ctx"]
    assert rec["audio_s"] == 9.5
    assert "mode" in rec


@pytest.mark.asyncio
async def test_fetch_dj_interjection_logs_fixed_announcement_on_empty_llm(monkeypatch, tmp_path):
    _exclude(monkeypatch, "quick")
    cog = _make_cog(tmp_path=tmp_path)
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="")

    records = []
    monkeypatch.setattr(
        "dj_narration_log.log_dj_narration", lambda record: records.append(record)
    )
    monkeypatch.setattr(
        "dj_narration_log.probe_audio_seconds", AsyncMock(return_value=None)
    )

    await cog._fetch_dj_interjection_raw(_info(requester="大肚"))

    assert len(records) == 1
    rec = records[0]
    assert rec["source"] == "fixed_announcement"
    assert rec["disqualify"] == "空字串"


@pytest.mark.asyncio
async def test_fetch_dj_interjection_survives_log_failure(monkeypatch, tmp_path):
    _exclude(monkeypatch, "quick")
    cog = _make_cog(tmp_path=tmp_path)

    def _raise(record):
        raise RuntimeError("boom")

    monkeypatch.setattr("dj_narration_log.log_dj_narration", _raise)

    result = await cog._fetch_dj_interjection_raw(_info())
    assert isinstance(result, dict)
    assert "text" in result
