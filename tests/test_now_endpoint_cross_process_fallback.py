"""
tests/test_now_endpoint_cross_process_fallback.py
TDD：GET /now 跨進程橋接 fallback。

satellite 進程自己的 MusicCog 沒在播（車載/瀏覽器模式沒用）時，退回讀
now_playing_state.json（main_discord.py 真正在 Discord 播歌那個進程寫的）；
satellite 自己有在播（car puck 用自己本地 mixer）則本地優先，不被 stale 檔案蓋掉。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from now_playing_state import save_now_playing_state


def _make_vc(mc=None):
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = mc
    return vc


@pytest.mark.asyncio
async def test_now_falls_back_to_bridge_file_when_local_cog_idle(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=True, title="夜曲", by="大肚",
                            cover="http://x/y.jpg", palette=["#111111"],
                            queue=[{"title": "晴天", "by": "小明"}], path=path)
    app = build_text_app(_make_vc(mc=None), token=None, now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        assert resp.status == 200
        body = await resp.json()
        assert body["playing"] is True
        assert body["title"] == "夜曲"
        assert body["by"] == "大肚"
        assert body["palette"] == ["#111111"]
        assert body["queue"] == [{"title": "晴天", "by": "小明"}]


@pytest.mark.asyncio
async def test_now_returns_false_when_neither_local_nor_bridge_playing(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "now_playing_state.json")   # 不存在
    app = build_text_app(_make_vc(mc=None), token=None, now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        assert (await resp.json()) == {"playing": False}


@pytest.mark.asyncio
async def test_now_prefers_local_cog_over_stale_bridge_file(tmp_path):
    """car puck 用 satellite 自己的本地 mixer 播歌 → 本地才是即時真相，不被舊橋接檔蓋掉。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=True, title="舊的橋接殘留", by="不是我",
                            cover="", palette=[], path=path)
    mc = MagicMock()
    mc.stream_mode = True
    mc._current_stream_info = {"title": "本地正在播", "requested_by": "車上"}
    mc.stream_paused = False
    mc.stream_queue = []
    app = build_text_app(_make_vc(mc=mc), token=None, now_playing_state_path=path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        body = await resp.json()
        assert body["title"] == "本地正在播"
