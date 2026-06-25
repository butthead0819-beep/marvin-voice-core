"""主題歌單 Step 3b：MusicCog 觸發 gate（env + 冷卻 + 每晚上限 + 跨日重置）。

實際入隊/LLM/resolve 由 themed_playlist 模組測試 + 離線 dryrun 覆蓋；這裡只鎖 gate 邏輯，
因為它決定「會不會打付費 LLM、會不會在播放核心啟動主題歌單」。
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.music_memory = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


_NOW = 10_000_000.0  # 任意未來時戳


def test_themed_gate_closed_when_env_off(monkeypatch):
    monkeypatch.delenv("MARVIN_THEMED_PLAYLIST", raising=False)
    assert _make_cog()._themed_gate_open(_NOW) is False


def test_themed_gate_open_when_env_on_fresh(monkeypatch):
    monkeypatch.setenv("MARVIN_THEMED_PLAYLIST", "1")
    cog = _make_cog()
    cog._last_themed_set_ts = 0.0
    cog._themed_sets_tonight = 0
    assert cog._themed_gate_open(_NOW) is True


def test_themed_gate_closed_during_cooldown(monkeypatch):
    monkeypatch.setenv("MARVIN_THEMED_PLAYLIST", "1")
    cog = _make_cog()
    cog._themed_set_date = datetime.date.fromtimestamp(_NOW)
    cog._last_themed_set_ts = _NOW - 60  # 1 分鐘前 < 冷卻
    cog._themed_sets_tonight = 0
    assert cog._themed_gate_open(_NOW) is False


def test_themed_gate_closed_when_nightly_cap_hit(monkeypatch):
    monkeypatch.setenv("MARVIN_THEMED_PLAYLIST", "1")
    cog = _make_cog()
    cog._themed_set_date = datetime.date.fromtimestamp(_NOW)
    cog._last_themed_set_ts = 0.0
    cog._themed_sets_tonight = cog._THEMED_SET_NIGHTLY_CAP
    assert cog._themed_gate_open(_NOW) is False


def test_themed_gate_resets_count_on_new_day(monkeypatch):
    monkeypatch.setenv("MARVIN_THEMED_PLAYLIST", "1")
    cog = _make_cog()
    cog._themed_set_date = datetime.date(2020, 1, 1)  # 舊日期
    cog._themed_sets_tonight = cog._THEMED_SET_NIGHTLY_CAP
    cog._last_themed_set_ts = 0.0
    assert cog._themed_gate_open(_NOW) is True          # 新的一天 → 重置 → 開
    assert cog._themed_sets_tonight == 0


def test_themed_dj_text_uses_pick_reason():
    """主題歌單的歌 → 播放時用 LLM 策展寫的選歌理由當 DJ 播報詞；其餘歌回 ""（走原 DJ 詞）。"""
    from cogs.music_cog import MusicCog
    assert MusicCog._themed_dj_text({"_lane": "themed", "_pick_reason": "今晚聊到溝通，這首最對味"}) \
        == "今晚聊到溝通，這首最對味"
    assert MusicCog._themed_dj_text({"_lane": "long_tail", "_pick_reason": "x"}) == ""  # 非主題歌
    assert MusicCog._themed_dj_text({"_lane": "themed"}) == ""                         # 無理由
    assert MusicCog._themed_dj_text({}) == ""


def test_enqueue_themed_infos_returns_list_and_marks_fields(monkeypatch):
    """int→list 改動：回實際入隊的 info 清單（Step 5 落 record 要用）+ 標 set 欄位。"""
    cog = _make_cog()
    cog.stream_queue = []
    monkeypatch.setattr(cog, "_check_song_duplicate", lambda **k: False)
    infos = [
        {"url": "u1", "title": "歌一", "_pick_reason": "理由一"},
        {"url": "u2", "title": "歌二", "_pick_reason": "理由二"},
    ]
    out = cog._enqueue_themed_infos(infos, "主題X", "狗與露", [], MagicMock())
    assert len(out) == 2
    assert cog.stream_queue == out                       # 全部入隊
    assert out[0]["_round_first"] is True and out[1]["_round_first"] is False
    assert all(i["_lane"] == "themed" and i["_set_id"] == "主題X" for i in out)
    assert out[0]["requested_by"] == "Marvin推薦（為狗與露）"


def test_enqueue_themed_infos_skips_duplicates(monkeypatch):
    """佇列/正在播去重命中 → 跳過該首；_round_first 標在『實際入隊』的第一首。"""
    cog = _make_cog()
    cog.stream_queue = []
    calls = {"n": 0}

    def dup(**k):
        calls["n"] += 1
        return calls["n"] == 1                            # 第一首判重複、其餘放行

    monkeypatch.setattr(cog, "_check_song_duplicate", dup)
    out = cog._enqueue_themed_infos(
        [{"url": "u1", "title": "歌一"}, {"url": "u2", "title": "歌二"}],
        "T", "u", [], MagicMock())
    assert [i["title"] for i in out] == ["歌二"]
    assert out[0]["_round_first"] is True                 # 入隊第一首才算 round_first


@pytest.mark.asyncio
async def test_try_themed_set_noop_when_env_off(monkeypatch):
    """env off → _try_themed_set 直接回 0、不打 LLM、不碰佇列。"""
    monkeypatch.delenv("MARVIN_THEMED_PLAYLIST", raising=False)
    cog = _make_cog()
    cog.stream_queue = []
    n = await cog._try_themed_set(["狗與露"], [], "狗與露", MagicMock())
    assert n == 0
    assert cog.stream_queue == []
