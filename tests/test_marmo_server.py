"""
Tests for MarmoServer webhook — 7 paths per the engineering review test plan.

Framework: pytest + aiohttp.test_utils
No real Discord connection needed; VoiceController is mocked.
"""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def mock_vc():
    vc = MagicMock()
    vc.play_tts = AsyncMock()
    return vc


@pytest.fixture
async def client(mock_vc, aiohttp_client):
    # Import here so env vars set in tests take effect before module-level constants
    import importlib
    import marvin_voice_core.marmo_server as ms
    importlib.reload(ms)

    server = ms.MarmoServer(voice_controller=mock_vc)
    app = web.Application()
    app.router.add_post("/marmo-result", server._handle_result)
    return await aiohttp_client(app), server, mock_vc


# ── Auth ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_skip_no_token(aiohttp_client, mock_vc):
    """No MARMO_TOKEN set → request proceeds without X-Marmo-Token header."""
    os.environ.pop("MARMO_TOKEN", None)
    import importlib
    import marvin_voice_core.marmo_server as ms
    importlib.reload(ms)

    server = ms.MarmoServer(voice_controller=mock_vc)
    app = web.Application()
    app.router.add_post("/marmo-result", server._handle_result)
    c = await aiohttp_client(app)

    resp = await c.post("/marmo-result", json={"text": "hello"})
    assert resp.status == 200
    assert await resp.text() == "ok"


@pytest.mark.asyncio
async def test_auth_reject_wrong_token(aiohttp_client, mock_vc):
    """MARMO_TOKEN set, wrong token in header → 401."""
    os.environ["MARMO_TOKEN"] = "secret123"
    import importlib
    import marvin_voice_core.marmo_server as ms
    importlib.reload(ms)

    server = ms.MarmoServer(voice_controller=mock_vc)
    app = web.Application()
    app.router.add_post("/marmo-result", server._handle_result)
    c = await aiohttp_client(app)

    resp = await c.post(
        "/marmo-result",
        json={"text": "hello"},
        headers={"X-Marmo-Token": "wrongtoken"},
    )
    assert resp.status == 401
    mock_vc.play_tts.assert_not_called()
    os.environ.pop("MARMO_TOKEN", None)


# ── Text validation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_text_returns_400(aiohttp_client, mock_vc):
    """Empty text field → 400, play_tts NOT called."""
    os.environ.pop("MARMO_TOKEN", None)
    import importlib
    import marvin_voice_core.marmo_server as ms
    importlib.reload(ms)

    server = ms.MarmoServer(voice_controller=mock_vc)
    app = web.Application()
    app.router.add_post("/marmo-result", server._handle_result)
    c = await aiohttp_client(app)

    resp = await c.post("/marmo-result", json={"text": "   "})
    assert resp.status == 400
    mock_vc.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_valid_text_calls_play_tts(aiohttp_client, mock_vc):
    """Non-empty text → play_tts called with MARMO_VOICE and already_in_channel=True."""
    os.environ.pop("MARMO_TOKEN", None)
    os.environ["MARMO_VOICE"] = "en-US-GuyNeural"
    import importlib
    import marvin_voice_core.marmo_server as ms
    importlib.reload(ms)

    server = ms.MarmoServer(voice_controller=mock_vc)
    app = web.Application()
    app.router.add_post("/marmo-result", server._handle_result)
    c = await aiohttp_client(app)

    resp = await c.post("/marmo-result", json={"text": "Marmo here. Done."})
    assert resp.status == 200
    assert await resp.text() == "ok"

    await asyncio.sleep(0)  # let create_task fire
    mock_vc.play_tts.assert_awaited_once()
    call_kwargs = mock_vc.play_tts.call_args
    assert call_kwargs.kwargs.get("already_in_channel") is True
    assert call_kwargs.kwargs.get("voice") == "en-US-GuyNeural"


# ── Dedup ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_job_id_not_spoken_twice(aiohttp_client, mock_vc):
    """Same job_id sent twice → play_tts called once only, second returns 'duplicate'."""
    os.environ.pop("MARMO_TOKEN", None)
    import importlib
    import marvin_voice_core.marmo_server as ms
    importlib.reload(ms)

    server = ms.MarmoServer(voice_controller=mock_vc)
    app = web.Application()
    app.router.add_post("/marmo-result", server._handle_result)
    c = await aiohttp_client(app)

    resp1 = await c.post("/marmo-result", json={"text": "result", "job_id": "job-42"})
    assert resp1.status == 200
    assert await resp1.text() == "ok"

    resp2 = await c.post("/marmo-result", json={"text": "result", "job_id": "job-42"})
    assert resp2.status == 200
    assert await resp2.text() == "duplicate"

    await asyncio.sleep(0)
    assert mock_vc.play_tts.call_count == 1


# ── Startup ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_port_already_in_use_does_not_crash():
    """Port already in use → OSError caught, logged as warning, server unavailable but no crash."""
    import socket
    from unittest.mock import patch
    import marvin_voice_core.marmo_server as ms

    mock_vc = MagicMock()
    mock_vc.play_tts = AsyncMock()

    # Find a free port dynamically to avoid collisions with other tests
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    server = None
    try:
        blocker.bind(("127.0.0.1", free_port))
        blocker.listen(1)
        with patch.object(ms, "MARMO_PORT", free_port):
            server = ms.MarmoServer(voice_controller=mock_vc)
            await server.start()  # should not raise even though port is occupied
    finally:
        blocker.close()
        if server and server._runner:
            await server.stop()


# ── speak_via_marvin (Marmo-side helper) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_via_marvin_connection_refused():
    """speak_via_marvin() raises aiohttp.ClientError if Marvin webhook is down.
    This path is currently unhandled on the Marmo side — tracked in TODOS.md.
    This test documents the failure mode.
    """
    import aiohttp

    async def speak_via_marvin(text: str, job_id: str = ""):
        headers = {}
        async with aiohttp.ClientSession() as session:
            await session.post(
                "http://127.0.0.1:19999/marmo-result",  # deliberately wrong port
                json={"text": text, "job_id": job_id},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=1.0),
            )

    with pytest.raises(aiohttp.ClientError):
        await speak_via_marvin("test", job_id="job-99")
