"""tests/test_openai_compat_endpoints.py — 驗證 car_http_app 的 OpenAI 相容端點：
- POST /v1/audio/transcriptions（共用 stt_lock + macos_stt_v2_bin + Bearer token）
- POST /v1/chat/completions（SSE stream / 音樂控制指令直派 / 一般對話過濾 <think> 與 [[GESTURE]]）
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer


def _make_vc():
    vc = MagicMock()
    vc.stream_mode = False
    vc.user_emotion_cache = {}
    vc.get_online_members = MagicMock(return_value=["狗與露"])
    vc._strip_wake_word = MagicMock(side_effect=lambda s: s.replace("嘿馬文", "").strip(" ，,"))
    vc._detect_music_command = MagicMock(return_value=None)

    engine = MagicMock()
    engine.stt_lock = asyncio.Semaphore(1)
    engine._run_swift_stt = AsyncMock(return_value=("今天天氣如何", {}))
    engine.conv_buffer = MagicMock()
    engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    vc.bot.engine = engine

    router = MagicMock()
    router.atmosphere_tracker = MagicMock()

    async def _fake_stream(*args, **kwargs):
        yield "[[GLANCE]] "
        yield "天氣還行，"
        yield "但宇宙依然冰冷。"

    router.stream_fast_response = MagicMock(side_effect=_fake_stream)
    vc.bot.router = router
    vc.bot.cogs = {}
    return vc


@pytest.mark.asyncio
async def test_v1_transcriptions_uses_stt_lock_and_v2_and_bearer_auth():
    from car_http_app import build_text_app

    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        data = aiohttp.FormData()
        data.add_field("file", b"RIFFxxxxWAVEfmt ", filename="test.wav", content_type="audio/wav")
        data.add_field("model", "marvin-swift-stt")
        resp = await client.post(
            "/v1/audio/transcriptions",
            data=data,
            headers={"Authorization": "Bearer s3cret"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"text": "今天天氣如何"}

    vc.bot.engine._run_swift_stt.assert_awaited_once()
    call_kwargs = vc.bot.engine._run_swift_stt.call_args.kwargs
    assert call_kwargs["is_wake_check"] is False
    assert call_kwargs["v2"] is True


@pytest.mark.asyncio
async def test_v1_transcriptions_rejects_bad_bearer_token():
    from car_http_app import build_text_app

    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/audio/transcriptions",
            data=b"RIFFxxxxWAVE",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status == 401
    vc.bot.engine._run_swift_stt.assert_not_awaited()


@pytest.mark.asyncio
async def test_v1_chat_completions_streams_sse_and_strips_gesture():
    from car_http_app import build_text_app

    vc = _make_vc()
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "marvin",
                "stream": True,
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "嘿馬文，今天天氣如何"},
                ],
            },
            headers={"Authorization": "Bearer s3cret"},
        )
        assert resp.status == 200
        raw_sse = (await resp.read()).decode("utf-8")

    chunks = []
    done_seen = False
    for line in raw_sse.splitlines():
        if not line.startswith("data: "):
            continue
        payload_str = line[6:].strip()
        if payload_str == "[DONE]":
            done_seen = True
            break
        obj = json.loads(payload_str)
        delta = obj["choices"][0]["delta"].get("content", "")
        if delta:
            chunks.append(delta)

    assert done_seen is True
    assert "".join(chunks) == "天氣還行，但宇宙依然冰冷。"


@pytest.mark.asyncio
async def test_v1_chat_completions_dispatches_music_command_without_radio_ack():
    from car_http_app import build_text_app

    vc = _make_vc()
    mc = MagicMock()
    mc._handle_voice_music_command = AsyncMock()
    vc.bot.cogs = {"MusicCog": mc}

    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "marvin",
                "stream": False,
                "messages": [{"role": "user", "content": "放一首周杰倫的晴天"}],
            },
            headers={"Authorization": "Bearer s3cret"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["choices"][0]["message"]["content"] == "好，幫你排進去了。"
        await asyncio.sleep(0.05)

    mc._handle_voice_music_command.assert_awaited_once_with(
        "狗與露", "放一首周杰倫的晴天", "play", play_ack=False
    )
    vc.bot.router.stream_fast_response.assert_not_called()

