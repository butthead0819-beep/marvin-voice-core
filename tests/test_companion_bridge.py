"""
CompanionBridge 測試 — WebSocket 雙向橋接 13 條路徑。

Lane B：companion-server 連到 Marvin 端的 bridge，
透過 aiohttp WS 交換典型 JSON 事件。

慣例：使用 MagicMock + AsyncMock 模擬 AtmosphereTracker / VectorStore /
MusicMemory / SukiMemory / VoiceController。
"""
import asyncio
import json
import socket

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_tracker():
    t = MagicMock()
    snap = MagicMock()
    snap.dominant_topic = "casual"
    snap.topic_confidence = 1.0
    snap.room_mood = "放鬆閒聊"
    snap.speaker_states = {"Jack": "normal"}
    snap.recent_topics = ["casual"]
    snap.ts = 1234567890.0
    t.get_snapshot.return_value = snap
    t.record_correction = MagicMock()
    return t


@pytest.fixture
def mock_vector_store():
    vs = MagicMock()
    vs.get_all.return_value = [
        {"id": "doc1", "document": "I love beer", "metadata": {"speaker": "Jack"}},
        {"id": "doc2", "document": "tired today", "metadata": {"speaker": "Jack"}},
    ]
    vs.delete = MagicMock()
    vs.update = MagicMock()
    return vs


@pytest.fixture
def mock_music_memory():
    mm = MagicMock()
    mm.get_top_songs_for_user.return_value = []
    mm.get_user_music_context.return_value = ""
    mm._data = {"songs": {}, "recommendations": {}}
    return mm


@pytest.fixture
def mock_music_engine():
    me = MagicMock()
    me.queue_request = AsyncMock()
    me.skip = AsyncMock()
    return me


@pytest.fixture
def mock_suki_memory():
    sm = MagicMock()
    sm.get_player_memory.return_value = {
        "personal_info": {"name": "Jack"},
        "likes": ["beer"],
        "dislikes": [],
    }
    return sm


@pytest.fixture
def mock_voice_controller():
    vc = MagicMock()
    vc.play_tts = AsyncMock()
    return vc


@pytest.fixture
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def bridge(monkeypatch, mock_tracker, mock_vector_store, mock_music_memory,
                 mock_suki_memory, mock_voice_controller, mock_music_engine, free_port):
    monkeypatch.setenv("MARMO_TOKEN", "test-token")
    import importlib
    import marvin_voice_core.companion_bridge as cb
    importlib.reload(cb)

    b = cb.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
        voice_controller=mock_voice_controller,
        music_engine=mock_music_engine,
        guild_id=42,
    )
    await b.start(host="127.0.0.1", port=free_port)
    yield b, free_port
    await b.stop()


async def _connect(port: int, token: str = "test-token"):
    """以 aiohttp client 建立 WS 連線（auth header）。
    自動排掉 on-connect voice_channel_snapshot，讓後續測試直接收業務事件。
    """
    import aiohttp
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(
        f"http://127.0.0.1:{port}/companion-ws",
        headers={"X-Marmo-Token": token},
    )
    # Drain the on-connect snapshot (sent by _handle_client since companion_bridge refactor)
    try:
        await asyncio.wait_for(ws.receive(), timeout=0.5)
    except asyncio.TimeoutError:
        pass
    return session, ws


# ── 1. Lifecycle ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_stop_lifecycle(monkeypatch, mock_tracker, mock_vector_store,
                                    mock_music_memory, mock_suki_memory, free_port):
    """start() 綁定 port，stop() 釋放。"""
    monkeypatch.setenv("MARMO_TOKEN", "tok")
    import importlib
    import marvin_voice_core.companion_bridge as cb
    importlib.reload(cb)

    b = cb.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
    )
    await b.start(host="127.0.0.1", port=free_port)
    # port 應該被占用
    with pytest.raises(OSError):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind(("127.0.0.1", free_port))
        finally:
            s.close()
    await b.stop()


# ── 2. Auth ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unauthorized_connection_rejected(bridge):
    """錯誤 token → 401。"""
    import aiohttp
    _, port = bridge
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc_info:
            await session.ws_connect(
                f"http://127.0.0.1:{port}/companion-ws",
                headers={"X-Marmo-Token": "wrong"},
            )
        assert exc_info.value.status == 401


@pytest.mark.asyncio
async def test_authorized_connection_accepts(bridge):
    """正確 token → 連上。"""
    _, port = bridge
    session, ws = await _connect(port)
    try:
        assert not ws.closed
    finally:
        await ws.close()
        await session.close()


# ── 4. Broadcast: stt_chunk ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_stt_chunk_broadcasts(bridge):
    """1 client：emit_stt_chunk → 收到對應 JSON。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        # 確認連線完成註冊
        await asyncio.sleep(0.05)
        await b.emit_stt_chunk(speaker="Jack", text="hello", engine="Swift")

        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "stt_chunk"
        assert data["payload"]["speaker"] == "Jack"
        assert data["payload"]["text"] == "hello"
        assert data["payload"]["engine"] == "Swift"
        assert "ts" in data
    finally:
        await ws.close()
        await session.close()


# ── 5. Multi-client broadcast ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_clients_all_receive_broadcast(bridge):
    """2 client 都應收到。"""
    b, port = bridge
    s1, ws1 = await _connect(port)
    s2, ws2 = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_tts_started(text="嗨", voice="zh-TW-HsiaoChenNeural", target=None)

        m1 = await asyncio.wait_for(ws1.receive(), timeout=2.0)
        m2 = await asyncio.wait_for(ws2.receive(), timeout=2.0)
        d1 = json.loads(m1.data)
        d2 = json.loads(m2.data)
        assert d1["type"] == "tts_started"
        assert d2["type"] == "tts_started"
        assert d1["payload"]["text"] == "嗨"
    finally:
        await ws1.close()
        await ws2.close()
        await s1.close()
        await s2.close()


# ── 6. Disconnect cleanup ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_client_disconnect_cleans_registry(bridge):
    """client 斷線：下次 emit 不應 raise。"""
    b, port = bridge
    session, ws = await _connect(port)
    await asyncio.sleep(0.05)
    await ws.close()
    await session.close()
    # 給 server 時間清理
    await asyncio.sleep(0.1)
    # emit 必須不爆
    await b.emit_tts_done()
    assert b.is_connected is False


# ── 7. atmosphere_feedback ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_atmosphere_feedback_handler_calls_tracker(bridge, mock_tracker):
    """收 ATMOSPHERE_FEEDBACK → tracker.record_correction 被以正確參數呼叫。"""
    _, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        msg = {
            "type": "atmosphere_feedback",
            "payload": {"snapshot_ts": 1234.0, "label": "too_loud", "speaker": "Jack"},
            "ts": 9999.0,
        }
        await ws.send_str(json.dumps(msg))
        await asyncio.sleep(0.1)
        mock_tracker.record_correction.assert_called_once_with(1234.0, "too_loud", "Jack")
    finally:
        await ws.close()
        await session.close()


# ── 8. memory_list_request / response ────────────────────────────────────────

@pytest.mark.asyncio
async def test_memory_list_request_response_cycle(bridge, mock_suki_memory, mock_vector_store):
    """收 MEMORY_LIST_REQUEST → 回 MEMORY_LIST_RESPONSE，包含合併資料。"""
    _, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        req = {
            "type": "memory_list_request",
            "payload": {"speaker": "Jack", "guild_id": 42, "limit": 10},
            "ts": 1.0,
        }
        await ws.send_str(json.dumps(req))
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        resp = json.loads(msg.data)
        assert resp["type"] == "memory_list_response"
        assert resp["payload"]["speaker"] == "Jack"
        assert "profile" in resp["payload"]
        assert "memories" in resp["payload"]
        # profile.likes is now a list of {text, doc_id} objects
        like_texts = [item["text"] for item in resp["payload"]["profile"]["likes"]]
        assert "beer" in like_texts
        assert len(resp["payload"]["memories"]) == 2
        mock_suki_memory.get_player_memory.assert_called_with("Jack")
        mock_vector_store.get_all.assert_called_with("Jack", 42, 10)
    finally:
        await ws.close()
        await session.close()


# ── 9. memory_delete ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_memory_delete_calls_vector_store(bridge, mock_vector_store):
    """收 MEMORY_DELETE → vector_store.delete 被呼叫。"""
    _, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        msg = {"type": "memory_delete", "payload": {"doc_id": "doc-123"}, "ts": 1.0}
        await ws.send_str(json.dumps(msg))
        await asyncio.sleep(0.1)
        mock_vector_store.delete.assert_called_once_with("doc-123")
    finally:
        await ws.close()
        await session.close()


# ── 10. memory_mark_uncertain ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_memory_mark_uncertain_calls_vector_store_update(bridge, mock_vector_store):
    """收 MEMORY_MARK_UNCERTAIN → vector_store.update(doc_id, {"uncertain": True})。"""
    _, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        msg = {"type": "memory_mark_uncertain", "payload": {"doc_id": "doc-9"}, "ts": 1.0}
        await ws.send_str(json.dumps(msg))
        await asyncio.sleep(0.1)
        mock_vector_store.update.assert_called_once_with("doc-9", {"uncertain": True})
    finally:
        await ws.close()
        await session.close()


# ── 11. mode_change ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode_change_persists_in_bridge(bridge):
    """收 MODE_CHANGE → bridge._mode 反映。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        msg = {"type": "mode_change", "payload": {"mode": "shutup"}, "ts": 1.0}
        await ws.send_str(json.dumps(msg))
        await asyncio.sleep(0.1)
        assert b._mode == "shutup"
    finally:
        await ws.close()
        await session.close()


# ── 12. atmosphere_snapshot emitter ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_atmosphere_snapshot_reads_tracker(bridge, mock_tracker):
    """emit_atmosphere_snapshot 呼叫 tracker.get_snapshot 並廣播。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_atmosphere_snapshot()
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "atmosphere_snapshot"
        assert data["payload"]["dominant_topic"] == "casual"
        assert data["payload"]["room_mood"] == "放鬆閒聊"
        mock_tracker.get_snapshot.assert_called()
    finally:
        await ws.close()
        await session.close()


# ── 13. unknown event type does not crash ───────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_event_type_logged_not_crash(bridge):
    """不認識的事件 → log，不 crash，後續 emit 仍 work。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        bad = {"type": "fake_event", "payload": {}, "ts": 1.0}
        await ws.send_str(json.dumps(bad))
        await asyncio.sleep(0.1)
        # 連線不該被關
        assert not ws.closed
        # 後續 emit 仍正常
        await b.emit_tts_done()
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "tts_done"
    finally:
        await ws.close()
        await session.close()


# ── 額外：tts_injection 走 voice_controller ─────────────────────────────────

@pytest.mark.asyncio
async def test_tts_injection_calls_voice_controller(bridge, mock_voice_controller):
    """收 TTS_INJECTION → voice_controller.play_tts 被呼叫。"""
    _, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        msg = {
            "type": "tts_injection",
            "payload": {"text": "嗨大家", "voice": "zh-TW-HsiaoChenNeural", "target": None},
            "ts": 1.0,
        }
        await ws.send_str(json.dumps(msg))
        await asyncio.sleep(0.1)
        mock_voice_controller.play_tts.assert_awaited()
        args = mock_voice_controller.play_tts.call_args
        assert args.kwargs.get("voice") == "zh-TW-HsiaoChenNeural"
    finally:
        await ws.close()
        await session.close()


# ── Lane E: Music emitters ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_music_started_broadcasts(bridge):
    """emit_music_started → 廣播 music_started 事件，payload 包含 title / target / source。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        song_info = {
            "title": "Midnight in Taipei",
            "style": "lo-fi",
            "target": "狗與露",
            "started_ts": 1234567890.0,
            "source": "suno",
        }
        await b.emit_music_started(song_info, requested_by="狗與露")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "music_started"
        assert data["payload"]["title"] == "Midnight in Taipei"
        assert data["payload"]["target"] == "狗與露"
        assert data["payload"]["source"] == "suno"
        assert data["payload"]["requested_by"] == "狗與露"
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_emit_music_ended_broadcasts(bridge):
    """emit_music_ended → 廣播 music_ended，含 completion。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_music_ended({"title": "Drive Home Slow"}, completion="natural")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "music_ended"
        assert data["payload"]["title"] == "Drive Home Slow"
        assert data["payload"]["completion"] == "natural"
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_emit_music_reaction_broadcasts(bridge):
    """emit_music_reaction → 廣播 music_reaction。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_music_reaction("Bob", {"title": "Midnight"}, "love")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "music_reaction"
        assert data["payload"]["username"] == "Bob"
        assert data["payload"]["reaction"] == "love"
        assert data["payload"]["title"] == "Midnight"
    finally:
        await ws.close()
        await session.close()


# ── Lane E: Incoming music handlers ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_music_play_request_calls_music_engine(bridge, mock_music_engine):
    """收 music_play_request → music_engine.queue_request 被呼叫。"""
    _, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        msg = {
            "type": "music_play_request",
            "payload": {"query": "Slow Train", "target": "Bob", "style": "lo-fi"},
            "ts": 1.0,
        }
        await ws.send_str(json.dumps(msg))
        await asyncio.sleep(0.1)
        mock_music_engine.queue_request.assert_awaited()
        kwargs = mock_music_engine.queue_request.call_args.kwargs
        assert kwargs.get("query") == "Slow Train"
        assert kwargs.get("target") == "Bob"
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_music_skip_calls_music_engine(bridge, mock_music_engine):
    """收 music_skip → music_engine.skip 被呼叫。"""
    _, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        msg = {"type": "music_skip", "payload": {}, "ts": 1.0}
        await ws.send_str(json.dumps(msg))
        await asyncio.sleep(0.1)
        mock_music_engine.skip.assert_awaited()
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_music_play_request_no_engine_does_not_crash(monkeypatch, mock_tracker,
                                                            mock_vector_store, mock_music_memory,
                                                            mock_suki_memory, free_port):
    """music_engine=None 時不爆。"""
    monkeypatch.setenv("MARMO_TOKEN", "tok-x")
    import importlib
    import marvin_voice_core.companion_bridge as cb
    importlib.reload(cb)

    b = cb.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
        music_engine=None,
    )
    await b.start(host="127.0.0.1", port=free_port)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            ws = await sess.ws_connect(
                f"http://127.0.0.1:{free_port}/companion-ws",
                headers={"X-Marmo-Token": "tok-x"},
            )
            try:
                await asyncio.sleep(0.05)
                req = {"type": "music_play_request", "payload": {"query": "x"}, "ts": 1.0}
                await ws.send_str(json.dumps(req))
                await asyncio.sleep(0.1)
                assert not ws.closed
            finally:
                await ws.close()
    finally:
        await b.stop()


# ── Lane E: MUSIC_RECOMMENDATIONS_REQUEST ───────────────────────────────────

@pytest.mark.asyncio
async def test_music_recommendations_request_returns_data(bridge, mock_music_memory):
    """收 music_recommendations_request → 查 MusicMemory，回 music_recommendations_response。"""
    _, port = bridge

    # 準備 music_memory 假資料
    mock_music_memory.get_top_songs_for_user.return_value = [
        {
            "title": "Midnight in Taipei",
            "uploader": "Suno",
            "url": "u1",
            "requesters": {"Jack": 3},
            "plays": [{"by": "Jack", "ts": 1.0, "time_slot": "深夜", "date": "2026-05-13"}],
            "reactions": {"Jack": {"feelings": ["放鬆"]}},
        },
        {
            "title": "Drive Home Slow",
            "uploader": "Suno",
            "url": "u2",
            "requesters": {"Jack": 1},
            "plays": [{"by": "Jack", "ts": 2.0, "time_slot": "傍晚", "date": "2026-05-12"}],
        },
    ]

    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        req = {
            "type": "music_recommendations_request",
            "payload": {"target_username": "Jack"},
            "ts": 1.0,
        }
        await ws.send_str(json.dumps(req))
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        resp = json.loads(msg.data)
        assert resp["type"] == "music_recommendations_response"
        assert resp["payload"]["target"] == "Jack"
        assert isinstance(resp["payload"]["recommendations"], list)
        assert isinstance(resp["payload"]["user_taste"], dict)
        assert isinstance(resp["payload"]["history"], list)
        # 歷史內含 title
        titles = [h.get("title") for h in resp["payload"]["history"]]
        assert "Midnight in Taipei" in titles
        mock_music_memory.get_top_songs_for_user.assert_called_with("Jack", limit=10)
    finally:
        await ws.close()
        await session.close()


# ── Lane F: Game phase emitter + force_skip / end handlers ───────────────────

@pytest.mark.asyncio
async def test_emit_game_phase_changed_broadcasts(bridge):
    """emit_game_phase_changed → 廣播 game_phase_changed，payload 包含 game / phase 與額外欄位。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_game_phase_changed(
            game_name="detective",
            phase="declaring",
            payload={
                "round": 2,
                "round_total": 5,
                "scoreboard": [{"user": "Jack", "score": 4}, {"user": "Marvin", "score": 3}],
                "current_player": "Bob",
                "timer_seconds": 60,
                "last_event": "輪到 Bob 宣告",
            },
        )
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "game_phase_changed"
        assert data["payload"]["game"] == "detective"
        assert data["payload"]["phase"] == "declaring"
        assert data["payload"]["round"] == 2
        assert data["payload"]["current_player"] == "Bob"
        assert data["payload"]["scoreboard"][0]["user"] == "Jack"
        assert data["payload"]["last_event"] == "輪到 Bob 宣告"
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_game_force_skip_round_calls_cog_method(monkeypatch, mock_tracker,
                                                     mock_vector_store, mock_music_memory,
                                                     mock_suki_memory, free_port):
    """收 game_force_skip_round → cog.force_skip_round 被呼叫。"""
    monkeypatch.setenv("MARMO_TOKEN", "tok-skip")
    import importlib
    import marvin_voice_core.companion_bridge as cb_mod
    importlib.reload(cb_mod)

    fake_cog = MagicMock()
    fake_cog.force_skip_round = AsyncMock()
    fake_cog.end_session = AsyncMock()

    def get_cog(name: str):
        if name == "DetectiveCog":
            return fake_cog
        return None

    b = cb_mod.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
        get_cog=get_cog,
    )
    await b.start(host="127.0.0.1", port=free_port)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            ws = await sess.ws_connect(
                f"http://127.0.0.1:{free_port}/companion-ws",
                headers={"X-Marmo-Token": "tok-skip"},
            )
            try:
                await asyncio.sleep(0.05)
                msg = {"type": "game_force_skip_round", "payload": {"game": "detective"}, "ts": 1.0}
                await ws.send_str(json.dumps(msg))
                await asyncio.sleep(0.1)
                fake_cog.force_skip_round.assert_awaited()
            finally:
                await ws.close()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_game_end_calls_cog_method(monkeypatch, mock_tracker,
                                        mock_vector_store, mock_music_memory,
                                        mock_suki_memory, free_port):
    """收 game_end → cog.end_session 被呼叫。"""
    monkeypatch.setenv("MARMO_TOKEN", "tok-end")
    import importlib
    import marvin_voice_core.companion_bridge as cb_mod
    importlib.reload(cb_mod)

    fake_cog = MagicMock()
    fake_cog.force_skip_round = AsyncMock()
    fake_cog.end_session = AsyncMock()

    b = cb_mod.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
        get_cog=lambda n: fake_cog if n == "DetectiveCog" else None,
    )
    await b.start(host="127.0.0.1", port=free_port)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            ws = await sess.ws_connect(
                f"http://127.0.0.1:{free_port}/companion-ws",
                headers={"X-Marmo-Token": "tok-end"},
            )
            try:
                await asyncio.sleep(0.05)
                msg = {"type": "game_end", "payload": {"game": "detective"}, "ts": 1.0}
                await ws.send_str(json.dumps(msg))
                await asyncio.sleep(0.1)
                fake_cog.end_session.assert_awaited()
            finally:
                await ws.close()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_game_handler_no_cog_logs_gracefully(monkeypatch, mock_tracker,
                                                  mock_vector_store, mock_music_memory,
                                                  mock_suki_memory, free_port):
    """get_cog 回 None（cog 未註冊）時不該爆，連線維持。"""
    monkeypatch.setenv("MARMO_TOKEN", "tok-nocog")
    import importlib
    import marvin_voice_core.companion_bridge as cb_mod
    importlib.reload(cb_mod)

    b = cb_mod.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
        get_cog=lambda n: None,
    )
    await b.start(host="127.0.0.1", port=free_port)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            ws = await sess.ws_connect(
                f"http://127.0.0.1:{free_port}/companion-ws",
                headers={"X-Marmo-Token": "tok-nocog"},
            )
            try:
                await asyncio.sleep(0.05)
                msg = {"type": "game_force_skip_round", "payload": {"game": "detective"}, "ts": 1.0}
                await ws.send_str(json.dumps(msg))
                await asyncio.sleep(0.1)
                # 連線維持
                assert not ws.closed
                # 後續 emit 仍 work
                await b.emit_tts_done()
                m2 = await asyncio.wait_for(ws.receive(), timeout=2.0)
                assert json.loads(m2.data)["type"] == "tts_done"
            finally:
                await ws.close()
    finally:
        await b.stop()


# ── Lane B2: Member presence emitters ───────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_member_joined_broadcasts(bridge):
    """emit_member_joined → 廣播 member_joined，payload 含 speaker / joined_ts，extras 會合併進去。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_member_joined(
            "Jack",
            payload_extras={"name": "狗與露", "marvin": False, "stage": "regular"},
        )
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "member_joined"
        assert data["payload"]["speaker"] == "Jack"
        assert "joined_ts" in data["payload"]
        assert data["payload"]["name"] == "狗與露"
        assert data["payload"]["marvin"] is False
        assert data["payload"]["stage"] == "regular"
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_emit_member_joined_without_extras(bridge):
    """無 extras 也能成功送出；payload 至少含 speaker 與 joined_ts。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_member_joined("Bob")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "member_joined"
        assert data["payload"]["speaker"] == "Bob"
        assert "joined_ts" in data["payload"]
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_emit_member_left_broadcasts(bridge):
    """emit_member_left → 廣播 member_left，payload 含 speaker / left_ts。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_member_left("Jack")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "member_left"
        assert data["payload"]["speaker"] == "Jack"
        assert "left_ts" in data["payload"]
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_emit_voice_channel_snapshot_broadcasts(bridge):
    """emit_voice_channel_snapshot → 廣播 voice_channel_snapshot，payload 含 members list 與 snapshot_ts。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        members = [
            {"speaker": "Jack", "name": "狗與露", "marvin": False},
            {"speaker": "Marvin", "name": "Marvin", "marvin": True},
        ]
        await b.emit_voice_channel_snapshot(members)
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "voice_channel_snapshot"
        assert "snapshot_ts" in data["payload"]
        assert isinstance(data["payload"]["members"], list)
        speakers = [m["speaker"] for m in data["payload"]["members"]]
        assert "Jack" in speakers
        assert "Marvin" in speakers
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_emit_voice_channel_snapshot_empty(bridge):
    """空 list 也能正確送出（沒人在頻道）。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        await b.emit_voice_channel_snapshot([])
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "voice_channel_snapshot"
        assert data["payload"]["members"] == []
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_music_recommendations_request_room_level(bridge, mock_music_memory):
    """target_username 為 None → 房間級別，target 回 'room'。"""
    _, port = bridge
    mock_music_memory.get_top_songs_for_user.return_value = []
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        req = {
            "type": "music_recommendations_request",
            "payload": {},
            "ts": 1.0,
        }
        await ws.send_str(json.dumps(req))
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        resp = json.loads(msg.data)
        assert resp["type"] == "music_recommendations_response"
        assert resp["payload"]["target"] == "room"
    finally:
        await ws.close()
        await session.close()


# ── Lane F2: game cog routing for busted / busted99 ─────────────────────────

@pytest.mark.asyncio
async def test_game_force_skip_round_routes_to_busted_cog(monkeypatch, mock_tracker,
                                                         mock_vector_store, mock_music_memory,
                                                         mock_suki_memory, free_port):
    """收 game_force_skip_round, game=busted → cog.force_skip_round 被呼叫。"""
    monkeypatch.setenv("MARMO_TOKEN", "tok-bu")
    import importlib
    import marvin_voice_core.companion_bridge as cb_mod
    importlib.reload(cb_mod)

    fake_cog = MagicMock()
    fake_cog.force_skip_round = AsyncMock()
    fake_cog.end_session = AsyncMock()

    b = cb_mod.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
        get_cog=lambda n: fake_cog if n == "BustedCog" else None,
    )
    await b.start(host="127.0.0.1", port=free_port)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            ws = await sess.ws_connect(
                f"http://127.0.0.1:{free_port}/companion-ws",
                headers={"X-Marmo-Token": "tok-bu"},
            )
            try:
                await asyncio.sleep(0.05)
                msg = {"type": "game_force_skip_round", "payload": {"game": "busted"}, "ts": 1.0}
                await ws.send_str(json.dumps(msg))
                await asyncio.sleep(0.1)
                fake_cog.force_skip_round.assert_awaited()
            finally:
                await ws.close()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_game_force_skip_round_routes_to_busted99_cog(monkeypatch, mock_tracker,
                                                            mock_vector_store, mock_music_memory,
                                                            mock_suki_memory, free_port):
    """收 game_force_skip_round, game=busted99 → cog.force_skip_round 被呼叫。"""
    monkeypatch.setenv("MARMO_TOKEN", "tok-b99")
    import importlib
    import marvin_voice_core.companion_bridge as cb_mod
    importlib.reload(cb_mod)

    fake_cog = MagicMock()
    fake_cog.force_skip_round = AsyncMock()
    fake_cog.end_session = AsyncMock()

    b = cb_mod.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
        get_cog=lambda n: fake_cog if n == "Busted99Cog" else None,
    )
    await b.start(host="127.0.0.1", port=free_port)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            ws = await sess.ws_connect(
                f"http://127.0.0.1:{free_port}/companion-ws",
                headers={"X-Marmo-Token": "tok-b99"},
            )
            try:
                await asyncio.sleep(0.05)
                msg = {"type": "game_force_skip_round", "payload": {"game": "busted99"}, "ts": 1.0}
                await ws.send_str(json.dumps(msg))
                await asyncio.sleep(0.1)
                fake_cog.force_skip_round.assert_awaited()
            finally:
                await ws.close()
    finally:
        await b.stop()


# ── Lane F2: 防呆雷達 request_radar_veto / GAME_ALERT / GAME_ALERT_RESPONSE ──

@pytest.mark.asyncio
async def test_request_radar_veto_emits_alert(bridge):
    """request_radar_veto → 廣播 game_alert，payload 含 alert_id / text / reason / severity。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        # background：發 request，timeout 短一點，不必等真的回
        task = asyncio.create_task(
            b.request_radar_veto(
                "Bob 真笨",
                {"risk": {"rule": "defeat_jab", "reason": "Bob 剛輸", "severity": "medium"}},
                timeout=0.3,
            )
        )
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        assert data["type"] == "game_alert"
        payload = data["payload"]
        assert "alert_id" in payload
        assert payload.get("text") == "Bob 真笨"
        assert payload.get("rule") == "defeat_jab"
        assert payload.get("severity") == "medium"
        assert payload.get("reason") == "Bob 剛輸"
        assert payload.get("timeout") == pytest.approx(0.3)
        # timeout 後 task 完成（default-safe = True）
        approved = await asyncio.wait_for(task, timeout=2.0)
        assert approved is True
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_request_radar_veto_returns_true_on_approval(bridge):
    """user 回 approve → request_radar_veto returns True。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        task = asyncio.create_task(
            b.request_radar_veto(
                "Bob 又輸了",
                {"risk": {"rule": "defeat_jab", "reason": "x", "severity": "medium"}},
                timeout=5.0,
            )
        )
        # 收到 alert 後抓 alert_id
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        alert_id = data["payload"]["alert_id"]
        # 回 approve
        resp = {
            "type": "game_alert_response",
            "payload": {"alert_id": alert_id, "decision": "approve"},
            "ts": 1.0,
        }
        await ws.send_str(json.dumps(resp))
        approved = await asyncio.wait_for(task, timeout=2.0)
        assert approved is True
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_request_radar_veto_returns_false_on_veto(bridge):
    """user 回 veto → request_radar_veto returns False。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        task = asyncio.create_task(
            b.request_radar_veto(
                "Bob 真聰明",
                {"risk": {"rule": "sarcasm_to_negative_bias_target", "reason": "x", "severity": "high"}},
                timeout=5.0,
            )
        )
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        data = json.loads(msg.data)
        alert_id = data["payload"]["alert_id"]
        resp = {
            "type": "game_alert_response",
            "payload": {"alert_id": alert_id, "decision": "veto"},
            "ts": 1.0,
        }
        await ws.send_str(json.dumps(resp))
        approved = await asyncio.wait_for(task, timeout=2.0)
        assert approved is False
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_request_radar_veto_returns_true_on_timeout(bridge):
    """user 沒回 → timeout 後 returns True（default-safe）。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        approved = await b.request_radar_veto(
            "嗨", {"risk": {"rule": "x", "reason": "y", "severity": "medium"}},
            timeout=0.2,
        )
        assert approved is True
    finally:
        await ws.close()
        await session.close()


@pytest.mark.asyncio
async def test_request_radar_veto_no_clients_returns_true(monkeypatch, mock_tracker,
                                                          mock_vector_store, mock_music_memory,
                                                          mock_suki_memory, free_port):
    """無 client 連線時 → 不等待，回 True（default-safe on disconnect）。"""
    monkeypatch.setenv("MARMO_TOKEN", "tok-nc")
    import importlib
    import marvin_voice_core.companion_bridge as cb_mod
    importlib.reload(cb_mod)
    b = cb_mod.CompanionBridge(
        atmosphere_tracker=mock_tracker,
        vector_store=mock_vector_store,
        music_memory=mock_music_memory,
        suki_memory=mock_suki_memory,
    )
    await b.start(host="127.0.0.1", port=free_port)
    try:
        approved = await b.request_radar_veto(
            "嗨", {"risk": {"rule": "x", "reason": "y", "severity": "low"}}, timeout=0.5
        )
        assert approved is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_game_alert_response_unknown_id_does_not_crash(bridge):
    """收到 game_alert_response 但 alert_id 不存在 → log 丟棄不 crash。"""
    b, port = bridge
    session, ws = await _connect(port)
    try:
        await asyncio.sleep(0.05)
        resp = {
            "type": "game_alert_response",
            "payload": {"alert_id": "no-such-id", "decision": "veto"},
            "ts": 1.0,
        }
        await ws.send_str(json.dumps(resp))
        await asyncio.sleep(0.1)
        # 連線維持，可繼續 emit
        assert not ws.closed
        await b.emit_tts_done()
        m = await asyncio.wait_for(ws.receive(), timeout=2.0)
        assert json.loads(m.data)["type"] == "tts_done"
    finally:
        await ws.close()
        await session.close()
