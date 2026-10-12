"""
SukiTTS.stream_audio — Azure Speech 備援 + edge-tts 斷路器（2026-10-12）。

背景：微軟 edge 朗讀端點對 edge-tts 一律回 403（上游 bug，未修）。
新順序：edge Primary →（edge 未斷路才試）edge Secondary → Azure → macOS say。

不變式：
  - edge 遇 401/403（_is_edge_blocked）→ 設 _edge_down_until，斷路期間跳過
    Primary/Secondary 與 prewarm，直接走 Azure
  - edge 遇非 401/403 錯誤 → 照舊試 Secondary，不設斷路
  - Azure 遇 401/403/429 → 設 _azure_down_until，斷路期間跳過 Azure
  - Azure 遇其他錯誤（500/timeout/exception）→ 不設斷路，退 say
  - 沒設 AZURE_SPEECH_KEY → 不建 session，直接 say
"""
from __future__ import annotations

import asyncio
import aiohttp
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_tts(monkeypatch, key="fake-key", region="eastasia"):
    if key is None:
        monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    else:
        monkeypatch.setenv("AZURE_SPEECH_KEY", key)
    if region is None:
        monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    else:
        monkeypatch.setenv("AZURE_SPEECH_REGION", region)
    from tts_engine import SukiTTS
    return SukiTTS()


class _FakeStream:
    """模擬 edge_tts.Communicate().stream() async generator。"""
    def __init__(self, chunks=None, raise_exc=None):
        self._chunks = chunks or []
        self._raise = raise_exc

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        if self._raise:
            raise self._raise
        for c in self._chunks:
            yield c


def _make_communicate_factory(raise_exc=None, chunks=None):
    """每次呼叫都回傳新的 fake stream 實例；factory 本身記錄呼叫次數。"""
    inst = MagicMock()
    inst.stream = MagicMock(side_effect=lambda: _FakeStream(chunks, raise_exc))
    factory = MagicMock(return_value=inst)
    return factory, inst


def _make_fake_response(status, body=b""):
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_fake_session(status=200, body=b"azure-mp3-bytes", raise_exc=None):
    """patch target：aiohttp.ClientSession → 回傳 fake session，記錄 post() 呼叫。"""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    if raise_exc:
        session.post = MagicMock(side_effect=raise_exc)
    else:
        session.post = MagicMock(return_value=_make_fake_response(status, body))
    factory = MagicMock(return_value=session)
    return factory, session


def _edge_403():
    return aiohttp.WSServerHandshakeError(request_info=MagicMock(), history=(), status=403)


# ── 1. edge 403 → 同次呼叫內不試 Secondary，直接走 Azure ──────────────────────

@pytest.mark.asyncio
async def test_edge_403_skips_secondary_goes_to_azure(monkeypatch):
    tts = _make_tts(monkeypatch)
    comm_factory, _ = _make_communicate_factory(raise_exc=_edge_403())
    sess_factory, session = _make_fake_session(status=200, body=b"azure-audio")
    say_mock = AsyncMock(return_value=False)

    with patch("tts_engine.edge_tts.Communicate", comm_factory), \
         patch("tts_engine.aiohttp.ClientSession", sess_factory), \
         patch.object(tts, "_generate_marvin_macos_say", say_mock):
        chunks = [c async for c in tts.stream_audio("測試文字")]

    assert comm_factory.call_count == 1, "斷路後同一次呼叫內不該再試 Secondary"
    session.post.assert_called_once()
    assert b"".join(chunks) == b"azure-audio"
    say_mock.assert_not_called()
    import time
    assert tts._edge_down_until > time.monotonic()


# ── 2. 斷路期間第二次 stream_audio：完全不建 Communicate ─────────────────────

@pytest.mark.asyncio
async def test_edge_breaker_open_skips_communicate_entirely(monkeypatch):
    tts = _make_tts(monkeypatch)
    comm_factory, _ = _make_communicate_factory(raise_exc=_edge_403())
    sess_factory, session = _make_fake_session(status=200, body=b"azure-audio")
    say_mock = AsyncMock(return_value=False)

    with patch("tts_engine.edge_tts.Communicate", comm_factory), \
         patch("tts_engine.aiohttp.ClientSession", sess_factory), \
         patch.object(tts, "_generate_marvin_macos_say", say_mock):
        _ = [c async for c in tts.stream_audio("第一句")]
        assert comm_factory.call_count == 1

        _ = [c async for c in tts.stream_audio("第二句")]
        assert comm_factory.call_count == 1, "斷路期間第二次呼叫不該建 Communicate"
        assert session.post.call_count == 2


# ── 3. edge 非 403 錯誤 → 照舊試 Secondary，不設斷路 ──────────────────────────

@pytest.mark.asyncio
async def test_edge_non_blocking_error_still_tries_secondary(monkeypatch):
    tts = _make_tts(monkeypatch)
    comm_factory, _ = _make_communicate_factory(raise_exc=Exception("No audio was received"))
    sess_factory, session = _make_fake_session(status=200, body=b"azure-audio")
    say_mock = AsyncMock(return_value=False)

    with patch("tts_engine.edge_tts.Communicate", comm_factory), \
         patch("tts_engine.aiohttp.ClientSession", sess_factory), \
         patch.object(tts, "_generate_marvin_macos_say", say_mock), \
         patch("tts_engine.asyncio.sleep", AsyncMock()):
        _ = [c async for c in tts.stream_audio("測試文字")]

    assert comm_factory.call_count == 2, "非 403 錯誤應照舊試 Secondary"
    import time
    assert tts._edge_down_until <= time.monotonic(), "非 403 錯誤不該設斷路"


# ── 4. Azure 回 429 → 退 say + 設斷路；下次不再打 Azure ──────────────────────

@pytest.mark.asyncio
async def test_azure_429_sets_breaker_and_falls_back_to_say(monkeypatch):
    tts = _make_tts(monkeypatch)
    comm_factory, _ = _make_communicate_factory(raise_exc=_edge_403())
    sess_factory, session = _make_fake_session(status=429, body=b"")
    say_mock = AsyncMock(return_value=True)

    with patch("tts_engine.edge_tts.Communicate", comm_factory), \
         patch("tts_engine.aiohttp.ClientSession", sess_factory), \
         patch.object(tts, "_generate_marvin_macos_say", say_mock), \
         patch("tts_engine.os.path.exists", return_value=False):
        _ = [c async for c in tts.stream_audio("測試文字")]

    say_mock.assert_called_once()
    import time
    assert tts._azure_down_until > time.monotonic()

    # 第二次呼叫：edge 仍斷路，Azure 也斷路 → session.post 不該再被呼叫
    call_count_before = session.post.call_count
    with patch("tts_engine.edge_tts.Communicate", comm_factory), \
         patch("tts_engine.aiohttp.ClientSession", sess_factory), \
         patch.object(tts, "_generate_marvin_macos_say", say_mock), \
         patch("tts_engine.os.path.exists", return_value=False):
        _ = [c async for c in tts.stream_audio("第二句")]
    assert session.post.call_count == call_count_before, "Azure 斷路期間不該再打 session"


# ── 5. Azure 回 500 → 退 say，不設 Azure 斷路 ────────────────────────────────

@pytest.mark.asyncio
async def test_azure_500_falls_back_to_say_without_breaker(monkeypatch):
    tts = _make_tts(monkeypatch)
    comm_factory, _ = _make_communicate_factory(raise_exc=_edge_403())
    sess_factory, session = _make_fake_session(status=500, body=b"")
    say_mock = AsyncMock(return_value=True)

    with patch("tts_engine.edge_tts.Communicate", comm_factory), \
         patch("tts_engine.aiohttp.ClientSession", sess_factory), \
         patch.object(tts, "_generate_marvin_macos_say", say_mock), \
         patch("tts_engine.os.path.exists", return_value=False):
        _ = [c async for c in tts.stream_audio("測試文字")]

    say_mock.assert_called_once()
    import time
    assert tts._azure_down_until <= time.monotonic(), "500 不該設斷路"


# ── 6. 沒設 AZURE_SPEECH_KEY → 不建 session，直接 say ────────────────────────

@pytest.mark.asyncio
async def test_no_azure_key_skips_session_goes_to_say(monkeypatch):
    tts = _make_tts(monkeypatch, key=None)
    comm_factory, _ = _make_communicate_factory(raise_exc=_edge_403())
    sess_factory, session = _make_fake_session(status=200, body=b"azure-audio")
    say_mock = AsyncMock(return_value=True)

    with patch("tts_engine.edge_tts.Communicate", comm_factory), \
         patch("tts_engine.aiohttp.ClientSession", sess_factory), \
         patch.object(tts, "_generate_marvin_macos_say", say_mock), \
         patch("tts_engine.os.path.exists", return_value=False):
        _ = [c async for c in tts.stream_audio("測試文字")]

    sess_factory.assert_not_called()
    say_mock.assert_called_once()


# ── 7. SSML / headers / URL 內容檢查 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_azure_request_ssml_and_headers(monkeypatch):
    """直接測 _stream_azure（stream_audio 的 _clean_text 會先濾掉 & < > 等符號，
    要驗證 SSML escape 行為必須繞過那層，直接餵含特殊字元的文字）。"""
    tts = _make_tts(monkeypatch, key="my-secret-key", region="eastasia")
    sess_factory, session = _make_fake_session(status=200, body=b"audio-bytes")

    with patch("tts_engine.aiohttp.ClientSession", sess_factory):
        chunks = [c async for c in tts._stream_azure("A&B<C>", tts.voice, tts.rate, tts.pitch)]

    assert b"".join(chunks) == b"audio-bytes"
    session.post.assert_called_once()
    call_args, call_kwargs = session.post.call_args
    url = call_args[0] if call_args else call_kwargs.get("url")
    assert "eastasia" in url
    assert "cognitiveservices/v1" in url

    headers = call_kwargs["headers"]
    assert headers["Ocp-Apim-Subscription-Key"] == "my-secret-key"
    assert headers["Content-Type"] == "application/ssml+xml"
    assert "audio-24khz-48kbitrate-mono-mp3" in headers["X-Microsoft-OutputFormat"]

    data = call_kwargs.get("data")
    assert "&amp;" in data and "&lt;" in data and "&gt;" in data, "特殊字元應被 XML escape"
    assert "A&B<C>" not in data
    assert 'xml:lang="zh-TW"' in data
    assert "zh-TW-YunJheNeural" in data
    assert "rate=" in data and "pitch=" in data


# ── 8. prewarm 在 edge 斷路期間不建 Communicate ──────────────────────────────

@pytest.mark.asyncio
async def test_prewarm_skips_when_edge_breaker_open(monkeypatch):
    tts = _make_tts(monkeypatch)
    import time
    tts._edge_down_until = time.monotonic() + 600.0
    comm_factory, _ = _make_communicate_factory(chunks=[{"type": "audio", "data": b"x"}])

    with patch("tts_engine.edge_tts.Communicate", comm_factory):
        await tts.prewarm()

    comm_factory.assert_not_called()
