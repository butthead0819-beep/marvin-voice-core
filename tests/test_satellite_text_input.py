"""
tests/test_satellite_text_input.py
TDD：驗 car_http_app.py 文字注入接口（HTTP）+ Siri HTTP endpoint。
無網路（aiohttp TestServer 起在本機隨機埠）、無 Discord、vc 用 AsyncMock。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    return vc


# --- 共用注入函式 ---
@pytest.mark.asyncio
async def test_inject_text_calls_handle_stt_result_as_transcribed():
    from car_http_app import inject_text
    vc = _make_vc()
    await inject_text(vc, "狗與露", "下一首")
    vc.handle_stt_result.assert_awaited_once()
    kw = vc.handle_stt_result.call_args.kwargs
    assert kw["speaker"] == "狗與露"
    assert kw["raw_text"] == "下一首"
    assert kw["wav_bytes"] == b""       # 文字模式無音訊
    assert kw["bypass_etd"] is True     # 文字輸入跳過語意終止檢測
    assert kw["is_wake_check"] is False
    assert kw["is_text_input"] is True  # 跳過 Echo Guard + 不等後續語音


@pytest.mark.asyncio
async def test_inject_text_uses_wallclock_timestamp():
    """回歸：timestamp 必須是牆鐘 time.time()，非單調 loop.time()。

    下游 Stale Drop 是 time.time()-timestamp；傳單調時鐘會被誤判排隊上億秒而丟棄。
    """
    import time as _time
    from car_http_app import inject_text
    vc = _make_vc()
    before = _time.time()
    await inject_text(vc, "狗與露", "下一首")
    after = _time.time()
    ts = vc.handle_stt_result.call_args.kwargs["timestamp"]
    assert before <= ts <= after


@pytest.mark.asyncio
async def test_inject_text_skips_empty_and_whitespace():
    from car_http_app import inject_text
    vc = _make_vc()
    await inject_text(vc, "狗與露", "   ")
    vc.handle_stt_result.assert_not_awaited()


# --- Siri 點歌 GET /play（伺服器補「放一首」，捷徑只要一格 URL）---
@pytest.mark.asyncio
async def test_http_play_prepends_放一首():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/play?q=告白氣球&t=s3cret")
        assert resp.status == 200
        assert (await resp.json())["text"] == "放一首告白氣球"
    assert vc.handle_stt_result.call_args.kwargs["raw_text"] == "放一首告白氣球"


@pytest.mark.asyncio
async def test_http_play_no_double_prefix_when_already_放一首():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/play?q=放一首七里香&t=s3cret")
        assert (await resp.json())["text"] == "放一首七里香"


@pytest.mark.asyncio
async def test_http_play_normalizes_bare_放():
    """裸「放X」不夠強（記憶），統一補成「放一首X」。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/play?q=放告白氣球&t=s3cret")
        assert (await resp.json())["text"] == "放一首告白氣球"


@pytest.mark.asyncio
async def test_http_play_rejects_wrong_token():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/play?q=告白氣球&t=wrong")
        assert resp.status == 401
    vc.handle_stt_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_play_empty_q_returns_400():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/play?q=&t=s3cret")
        assert resp.status == 400
    vc.handle_stt_result.assert_not_awaited()


# --- Siri HTTP endpoint ---
@pytest.mark.asyncio
async def test_http_say_injects_text_and_returns_ok():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/say", data="放周杰倫".encode(),
            headers={"X-Marvin-Token": "s3cret"})
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
    vc.handle_stt_result.assert_awaited_once()
    kw = vc.handle_stt_result.call_args.kwargs
    assert kw["raw_text"] == "放周杰倫"
    assert kw["speaker"] == "狗與露"


@pytest.mark.asyncio
async def test_http_say_rejects_wrong_token():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/say", data=b"hi", headers={"X-Marvin-Token": "wrong"})
        assert resp.status == 401
    vc.handle_stt_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_say_rejects_empty_text():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/say", data="   ".encode(), headers={"X-Marvin-Token": "s3cret"})
        assert resp.status == 400
    vc.handle_stt_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_say_no_token_configured_allows_request():
    """token=None（Tailscale 私網）→ 不驗證，直接放行。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token=None, default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/say", data="測試".encode())
        assert resp.status == 200
    vc.handle_stt_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_now_reports_current_song_from_bridge_file(tmp_path):
    """HUD 只在家用，/now 要跟橋接檔（Pi satellite／main_discord.py 真正播放狀態）連動。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    from now_playing_state import save_now_playing_state
    vc = _make_vc()
    path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=True, title="告白氣球", by="狗與露",
                            cover="https://i.ytimg.com/vi/abc/hq.jpg", path=path)
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露",
                          now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now?t=s3cret")
        assert resp.status == 200
        body = await resp.json()
        assert body["playing"] is True
        assert body["title"] == "告白氣球"
        assert body["by"] == "狗與露"
        assert body["cover"] == "https://i.ytimg.com/vi/abc/hq.jpg"


@pytest.mark.asyncio
async def test_http_now_ignores_local_music_cog(tmp_path):
    """satellite 進程自己的 MusicCog（car puck／瀏覽器 satellite 在外播放）不該蓋掉家用 HUD。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    mc = MagicMock()
    mc.stream_mode = True
    mc.stream_paused = False
    mc._current_stream_info = {"title": "在外本地播放", "requested_by": "車上", "thumbnail": ""}
    mc.stream_queue = [{"title": "下一首A", "requested_by": "阿明"}]
    mc._current_stream_start_time = None
    mc._current_stream_comment = None
    vc.bot.cogs.get.return_value = mc
    path = str(tmp_path / "now_playing_state.json")   # 橋接檔不存在＝家裡沒在播
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露",
                          now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/now?t=s3cret")).json()
        assert body == {"playing": False}


@pytest.mark.asyncio
async def test_http_now_reports_not_playing_when_idle(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    mc = MagicMock()
    mc.stream_mode = False
    mc._current_stream_info = None
    vc.bot.cogs.get.return_value = mc
    # 不隔離會讀到真 bot 進程正在寫的 now_playing_state.json（跨進程橋接檔），
    # bot 真的在播歌時這條就會假紅——bridge fallback 分支必須指到隔離的 tmp 檔。
    path = str(tmp_path / "now_playing_state.json")
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露",
                          now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now?t=s3cret")
        assert resp.status == 200
        assert (await resp.json())["playing"] is False


@pytest.mark.asyncio
async def test_http_say_token_via_query_param():
    """控制台網頁跨網域呼叫：token 走網址 ?t= 也要能過。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/say?t=s3cret", data="下一首".encode())
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    vc.handle_stt_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_say_options_preflight_returns_cors():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token="s3cret", default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.options("/say")
        assert resp.status == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


@pytest.mark.asyncio
async def test_http_say_json_speaker_override():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    vc = _make_vc()
    app = build_text_app(vc, token=None, default_speaker="狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/say", json={"text": "哈囉", "speaker": "showay"})
        assert resp.status == 200
    kw = vc.handle_stt_result.call_args.kwargs
    assert kw["raw_text"] == "哈囉"
    assert kw["speaker"] == "showay"
