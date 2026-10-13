"""
tests/test_satellite_audio_ingest.py
TDD：車載/裝置端的音訊入口（Mac :8790）。

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
    from car_http_app import inject_audio
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
    from car_http_app import inject_audio
    vc = _make_vc()
    assert await inject_audio(vc, b"") is False
    vc.bot.engine._run_swift_stt.assert_not_awaited()
    vc.handle_stt_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_inject_audio_empty_transcript_skips_reply():
    from car_http_app import inject_audio
    vc = _make_vc(stt_text="")          # STT 無結果（雜訊）
    assert await inject_audio(vc, b"RIFFfake") is False
    vc.handle_stt_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_inject_audio_require_wake_with_wake_word_proceeds():
    """車機暫停鍵觸發（require_wake=True）但 STT 文字含喚醒詞 → 照常注入。"""
    from car_http_app import inject_audio
    vc = _make_vc(stt_text="馬文現在幾點")
    ok = await inject_audio(vc, b"RIFFfake-wav", require_wake=True)
    assert ok is True
    vc.handle_stt_result.assert_awaited_once()
    assert vc.handle_stt_result.call_args.kwargs["raw_text"] == "馬文現在幾點"


@pytest.mark.asyncio
async def test_inject_audio_require_wake_without_wake_word_discards(caplog):
    """車機自己送 PAUSE（來電等）錄到的內容沒喚醒詞 → 丟棄，且不記錄原文。"""
    import logging
    from car_http_app import inject_audio
    # car_http_app 的丟棄訊息是 INFO；不設等級 caplog 抓不到任何東西，「原文不在 log」會恆真。
    caplog.set_level(logging.INFO, logger="car_http_app")
    vc = _make_vc(stt_text="我等一下就到")
    ok = await inject_audio(vc, b"RIFFfake-wav", require_wake=True)
    assert ok is False
    vc.handle_stt_result.assert_not_awaited()
    assert "無喚醒詞，丟棄" in caplog.text      # 證明 log 有抓到
    assert "我等一下就到" not in caplog.text


@pytest.mark.asyncio
async def test_inject_audio_default_no_require_wake_proceeds_without_wake_word():
    """實體 PTT 按鈕（require_wake 預設 False）沒喚醒詞也照常注入，行為不變。"""
    from car_http_app import inject_audio
    vc = _make_vc(stt_text="講個笑話")
    ok = await inject_audio(vc, b"RIFFfake-wav")
    assert ok is True
    vc.handle_stt_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_inject_audio_deletes_temp_wav():
    """守則：暫存 WAV 必須清除。"""
    from car_http_app import inject_audio
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
    from car_http_app import build_text_app
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
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=wrong", data=b"RIFFfake")
        assert resp.status == 401
    vc.bot.engine._run_swift_stt.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_audio_empty_body_returns_400():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
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
    from car_http_app import build_text_app
    vc = _make_vc(stt_text="")
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret", data=b"RIFFfake")
        assert resp.status == 200
        assert (await resp.json())["ok"] is False
    vc.handle_stt_result.assert_not_awaited()
