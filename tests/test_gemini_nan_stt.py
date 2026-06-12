"""Gemini 台語 STT shadow lane — 候選引擎影子比對（評估取代雅婷）。

背景（2026-06-12）：陳進文走雅婷 asr-zh-tw-std（台語→華語漢字），免費額度將盡，
全付費 ≈ NT$1,000+/月。候選解：Gemini Flash 收音訊直接轉華語（~US$1.5/月）。
品質未知 → shadow 模式收 3-5 天對照數據再決策（同 judge race 套路）。

契約：
- shadow 對主管線零影響：任何失敗只寫 error 欄位，不 raise、不阻塞
- env NAN_STT_SHADOW 閘控（預設 off），啟用狀態要 log（J2 空轉教訓）
- 抽樣率 NAN_STT_SHADOW_RATE 控量（預設 0.25，~100 句/天）
- 每筆記錄寫 records/nan_stt_shadow.jsonl：ts/speaker/yating/gemini/latency_ms/error
"""
from __future__ import annotations

import json
import wave
import io
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import gemini_nan_stt


# ── wav_from_float（pure）─────────────────────────────────────────────────

def test_wav_from_float_produces_valid_16k_mono_wav():
    audio = np.zeros(16000, dtype=np.float32)  # 1 秒靜音
    data = gemini_nan_stt.wav_from_float(audio)

    assert data[:4] == b"RIFF"
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 16000


def test_wav_from_float_empty_returns_empty_bytes():
    assert gemini_nan_stt.wav_from_float(None) == b""
    assert gemini_nan_stt.wav_from_float(np.array([], dtype=np.float32)) == b""


# ── transcribe（IO shell，注入 client）───────────────────────────────────

def _client_returning(text):
    client = MagicMock()
    resp = MagicMock()
    resp.text = text
    client.aio.models.generate_content = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_transcribe_returns_stripped_text():
    client = _client_returning("  今天天氣很好\n")
    audio = np.zeros(16000, dtype=np.float32)

    text = await gemini_nan_stt.transcribe(client, audio)

    assert text == "今天天氣很好"
    kwargs = client.aio.models.generate_content.await_args.kwargs
    assert "flash" in kwargs["model"]


@pytest.mark.asyncio
async def test_transcribe_prompt_mentions_nan_to_mandarin():
    """prompt 必須講清楚「台語語音→華語漢字」，否則模型會自由發揮。"""
    client = _client_returning("好")
    await gemini_nan_stt.transcribe(client, np.zeros(1600, dtype=np.float32))

    contents = client.aio.models.generate_content.await_args.kwargs["contents"]
    prompt_text = str(contents)
    assert "台語" in prompt_text or "閩南語" in prompt_text
    assert "華語" in prompt_text


@pytest.mark.asyncio
async def test_transcribe_failure_returns_empty():
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=RuntimeError("429"))

    text = await gemini_nan_stt.transcribe(client, np.zeros(1600, dtype=np.float32))

    assert text == ""


@pytest.mark.asyncio
async def test_transcribe_empty_audio_skips_api_call():
    client = _client_returning("不該被呼叫")

    text = await gemini_nan_stt.transcribe(client, None)

    assert text == ""
    client.aio.models.generate_content.assert_not_called()


# ── run_shadow（比對記錄）────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_shadow_writes_comparison_record(tmp_path):
    out = tmp_path / "nan_stt_shadow.jsonl"

    async def fake_transcribe(audio):
        return "甲飽未"

    await gemini_nan_stt.run_shadow(
        np.zeros(1600, dtype=np.float32), "陳進文", "呷飽未",
        transcribe_fn=fake_transcribe, out_path=out,
    )

    rec = json.loads(out.read_text(encoding="utf-8").strip())
    assert rec["speaker"] == "陳進文"
    assert rec["yating"] == "呷飽未"
    assert rec["gemini"] == "甲飽未"
    assert rec["error"] is None
    assert rec["latency_ms"] >= 0
    assert rec["ts"] > 1_000_000  # 真實時間戳，防測試污染誤判


@pytest.mark.asyncio
async def test_run_shadow_gemini_failure_records_error_not_raise(tmp_path):
    out = tmp_path / "nan_stt_shadow.jsonl"

    async def boom(audio):
        raise RuntimeError("network down")

    # 不該 raise（主管線零影響）
    await gemini_nan_stt.run_shadow(
        np.zeros(1600, dtype=np.float32), "陳進文", "呷飽未",
        transcribe_fn=boom, out_path=out,
    )

    rec = json.loads(out.read_text(encoding="utf-8").strip())
    assert rec["gemini"] == ""
    assert "network down" in rec["error"]


# ── maybe_shadow（env 閘 + 抽樣）─────────────────────────────────────────

def test_shadow_disabled_by_default(monkeypatch):
    monkeypatch.delenv("NAN_STT_SHADOW", raising=False)
    assert gemini_nan_stt.shadow_enabled() is False


def test_shadow_enabled_via_env(monkeypatch):
    monkeypatch.setenv("NAN_STT_SHADOW", "true")
    assert gemini_nan_stt.shadow_enabled() is True


def test_sample_rate_from_env(monkeypatch):
    monkeypatch.setenv("NAN_STT_SHADOW_RATE", "1.0")
    assert gemini_nan_stt.should_sample(rng=lambda: 0.99) is True
    monkeypatch.setenv("NAN_STT_SHADOW_RATE", "0.25")
    assert gemini_nan_stt.should_sample(rng=lambda: 0.5) is False
    assert gemini_nan_stt.should_sample(rng=lambda: 0.1) is True
