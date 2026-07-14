"""
tests/test_satellite_audio_ingest.py
TDD：純軟體 iOS satellite 的音訊入口（Mac :8790）。

與 Pi satellite（wyoming / process_audio_slice）完全解耦：
瀏覽器收音 → POST /audio → inject_audio → transcribe_hybrid → handle_stt_result。
無網路（aiohttp TestServer）、無 Discord、vc/stt_handler 用 Mock。
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_vc(stt_text="現在幾點"):
    """vc 帶 engine._run_swift_stt（編譯 STT 二進位，只回文字）+ handle_stt_result。"""
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.engine._run_swift_stt = AsyncMock(return_value=(stt_text, {}))
    return vc


# ── inject_audio：STT（編譯二進位）→ inject_text（強制回覆）──────────────────
@pytest.mark.asyncio
async def test_inject_audio_transcribes_then_forces_reply_via_text():
    from main_satellite import inject_audio
    vc = _make_vc(stt_text="現在幾點")
    ok = await inject_audio(vc, b"RIFFfake-wav")
    assert ok is True
    vc.bot.engine._run_swift_stt.assert_awaited_once()
    # 走 inject_text → handle_stt_result（is_text_input=True 強制回覆，就是 /say 那條）
    vc.handle_stt_result.assert_awaited_once()
    kw = vc.handle_stt_result.call_args.kwargs
    assert kw["raw_text"] == "現在幾點"
    assert kw["is_text_input"] is True     # 關鍵：沒喊喚醒詞也強制回覆
    assert kw["bypass_etd"] is True


@pytest.mark.asyncio
async def test_inject_audio_empty_bytes_skips_stt():
    from main_satellite import inject_audio
    vc = _make_vc()
    assert await inject_audio(vc, b"") is False
    vc.bot.engine._run_swift_stt.assert_not_awaited()
    vc.handle_stt_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_inject_audio_empty_transcript_skips_reply():
    from main_satellite import inject_audio
    vc = _make_vc(stt_text="")          # STT 無結果（雜訊）
    assert await inject_audio(vc, b"RIFFfake") is False
    vc.handle_stt_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_inject_audio_deletes_temp_wav():
    """守則：暫存 WAV 必須清除。"""
    from main_satellite import inject_audio
    vc = _make_vc()
    captured = {}

    async def _capture(path, *a, **k):
        captured["path"] = path
        assert os.path.exists(path)
        return ("你好", {})

    vc.bot.engine._run_swift_stt = AsyncMock(side_effect=_capture)
    await inject_audio(vc, b"RIFFfake")
    assert not os.path.exists(captured["path"])


# ── POST /audio HTTP endpoint ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_http_audio_posts_wav_and_triggers_reply():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    vc = _make_vc(stt_text="講個笑話")
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/audio", data=b"RIFFfake-wav",
            headers={"X-Marvin-Token": "s3cret", "Content-Type": "audio/wav"})
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
    vc.handle_stt_result.assert_awaited_once()
    assert vc.handle_stt_result.call_args.kwargs["raw_text"] == "講個笑話"


@pytest.mark.asyncio
async def test_http_audio_rejects_wrong_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=wrong", data=b"RIFFfake")
        assert resp.status == 401
    vc.bot.engine._run_swift_stt.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_audio_empty_body_returns_400():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret", data=b"")
        assert resp.status == 400
    vc.bot.engine._run_swift_stt.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_audio_no_speech_returns_ok_false():
    """收到音訊但 STT 無結果 → 200 ok:false，不回覆。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    vc = _make_vc(stt_text="")
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret", data=b"RIFFfake")
        assert resp.status == 200
        assert (await resp.json())["ok"] is False
    vc.handle_stt_result.assert_not_awaited()


# ── GET /satellite HTML（Mac 自服務、Pi 不碰）────────────────────────────
@pytest.mark.asyncio
async def test_http_satellite_serves_html_page():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/satellite?t=s3cret")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        html = await resp.text()
        assert "getUserMedia" in html   # 瀏覽器收音
        assert "/audio" in html         # 上傳入口


@pytest.mark.asyncio
async def test_http_satellite_injects_token_into_page():
    """token 要嵌進頁面，瀏覽器呼叫 /audio 才帶得上。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/satellite?t=s3cret")).text()
        assert "s3cret" in html


# ── GET /reply（馬文回覆的 TTS 音訊回傳瀏覽器）──────────────────────────────
class _FakeReplySource:
    def __init__(self, seq, wav):
        self._seq, self._wav = seq, wav
    def latest_wav(self):
        return self._seq, self._wav


@pytest.mark.asyncio
async def test_http_reply_returns_204_when_no_source():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")   # 無 reply_source
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/reply?t=s3cret&since=0")
        assert resp.status == 204


@pytest.mark.asyncio
async def test_http_reply_returns_wav_when_newer_seq():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    src = _FakeReplySource(3, b"RIFFxxxxWAVEdata")
    app = build_text_app(_make_vc(), token="s3cret", reply_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/reply?t=s3cret&since=2")
        assert resp.status == 200
        assert "audio/wav" in resp.headers["Content-Type"]
        assert resp.headers["X-Reply-Seq"] == "3"
        assert (await resp.read()) == b"RIFFxxxxWAVEdata"


@pytest.mark.asyncio
async def test_http_reply_returns_204_when_not_newer():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    src = _FakeReplySource(3, b"RIFFxxxxWAVEdata")
    app = build_text_app(_make_vc(), token="s3cret", reply_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/reply?t=s3cret&since=3")   # 已播過同一段
        assert resp.status == 204


@pytest.mark.asyncio
async def test_http_reply_rejects_wrong_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    src = _FakeReplySource(1, b"RIFFxxxxWAVEdata")
    app = build_text_app(_make_vc(), token="s3cret", reply_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/reply?t=wrong&since=0")
        assert resp.status == 401
