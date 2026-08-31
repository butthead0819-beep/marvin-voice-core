"""TDD: DJ crossfade 的「馬文式厭世冷笑話」低頻彩蛋。

規格（使用者訂）：
- 頻道安靜（非熱烈聊天）且距上次超過 30 分鐘冷卻 → 這輪 crossfade 換成厭世冷笑話。
- 笑話來源 = 策展笑話庫（joke_bank.py）：下一首歌名字音撞到某則笑話的 hook 就播那則，
  沒撞到就不講（fallback 回正常串場）。LLM 現編諧音梗品質不穩，已停用。
- 頻道熱烈聊天（active_chat）→ 不觸發，走平常路徑。
- 冷卻未到（剛觸發過）→ 不觸發，走平常路徑。
- themed 歌單自己有預先寫好的口白 → 不搶。
- 近期播過的笑話（最多 5 則）傳進 exclude，避免短期內重複。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog(online_members=None):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="平常串場台詞")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    from dj_topic_selector import TopicCooldownStore
    import tempfile
    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    cog._life_cores = MagicMock(return_value=[])
    cog._dj_topic_cooldown_store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))

    fake_vc = MagicMock()
    fake_vc.get_online_members = MagicMock(return_value=online_members or [])
    cog._vc = MagicMock(return_value=fake_vc)
    cog.bot.router.atmosphere_tracker = None
    return cog


def _info(requester="大肚"):
    return {"title": "夜曲", "uploader": "周杰倫", "requested_by": requester,
            "url": "https://example/x"}


def _patch_bank(monkeypatch, match_return):
    """把 joke_bank.get_joke_bank() 換成回傳固定值的 fake。回傳 fake bank 供斷言。"""
    fake_bank = MagicMock()
    fake_bank.match = MagicMock(return_value=match_return)
    monkeypatch.setattr("joke_bank.get_joke_bank", lambda: fake_bank)
    return fake_bank


JOKE = "稻草人站在田裡一整天什麼都沒做，還是被叫做人。……付出跟頭銜從來不成正比。"


@pytest.mark.asyncio
async def test_quiet_and_cooldown_elapsed_plays_bank_joke(monkeypatch):
    """安靜 + 冷卻已過 + 笑話庫命中 → 播那則笑話，不走 LLM。"""
    import time
    bank = _patch_bank(monkeypatch, JOKE)
    cog = _make_cog(online_members=["大肚"])
    cog._last_dj_joke_ts = time.time() - 3600

    dj = await cog._fetch_dj_interjection_raw(_info())

    assert dj["text"] == JOKE
    bank.match.assert_called_once()
    called = [c.args[0] for c in cog.bot.router.generate_dynamic_system_msg.await_args_list]
    assert "dj_joke_interjection" not in called


@pytest.mark.asyncio
async def test_bank_miss_falls_back_to_normal_segue(monkeypatch):
    """笑話庫沒命中（match 回 None）→ 不講笑話，走平常 dj_interjection。"""
    import time
    _patch_bank(monkeypatch, None)
    cog = _make_cog(online_members=["大肚"])
    cog._last_dj_joke_ts = time.time() - 3600
    before = cog._last_dj_joke_ts

    dj = await cog._fetch_dj_interjection_raw(_info())

    assert dj["text"] != JOKE
    assert cog._last_dj_joke_ts == before  # 沒命中不更新冷卻


@pytest.mark.asyncio
async def test_bank_hit_updates_cooldown_and_recent_list(monkeypatch):
    """命中後更新冷卻時間戳 + 把該則笑話記進 _recent_dj_jokes。"""
    import time
    _patch_bank(monkeypatch, JOKE)
    cog = _make_cog(online_members=["大肚"])
    cog._last_dj_joke_ts = time.time() - 3600
    before = cog._last_dj_joke_ts

    await cog._fetch_dj_interjection_raw(_info())

    assert cog._last_dj_joke_ts > before
    assert JOKE in cog._recent_dj_jokes


@pytest.mark.asyncio
async def test_recent_jokes_passed_as_exclude(monkeypatch):
    """近期播過的笑話要傳進 match(exclude=...)，讓庫跳過它們。"""
    import time
    bank = _patch_bank(monkeypatch, JOKE)
    cog = _make_cog(online_members=["大肚"])
    cog._last_dj_joke_ts = time.time() - 3600
    cog._recent_dj_jokes = ["舊笑話一", "舊笑話二"]

    await cog._fetch_dj_interjection_raw(_info())

    _, kwargs = bank.match.call_args
    assert kwargs["exclude"] == {"舊笑話一", "舊笑話二"}


@pytest.mark.asyncio
async def test_cooldown_not_elapsed_does_not_touch_bank(monkeypatch):
    """冷卻還沒到 → 根本不查笑話庫，走平常 dj_interjection。"""
    import time
    bank = _patch_bank(monkeypatch, JOKE)
    cog = _make_cog(online_members=["大肚"])
    cog._last_dj_joke_ts = time.time() - 60

    dj = await cog._fetch_dj_interjection_raw(_info())

    assert dj["text"] != JOKE
    bank.match.assert_not_called()


@pytest.mark.asyncio
async def test_active_chat_does_not_touch_bank(monkeypatch):
    """頻道熱烈聊天（active_chat）→ 即使冷卻已過也不查笑話庫。"""
    import time
    bank = _patch_bank(monkeypatch, JOKE)
    cog = _make_cog(online_members=["大肚", "狗與露", "Alice", "Bob"])
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(
        return_value=[
            {"speaker": "大肚", "text": "u1"},
            {"speaker": "狗與露", "text": "u2"},
            {"speaker": "Alice", "text": "u3"},
            {"speaker": "Bob", "text": "u4"},
        ]
    )
    cog._last_dj_joke_ts = time.time() - 3600

    await cog._fetch_dj_interjection_raw(_info())

    bank.match.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_cog_cooldown_starts_at_construction_not_zero(monkeypatch):
    """新建的 cog（剛開機/剛連上）不該立刻講笑話——冷卻起點是建構當下，不是 0。"""
    bank = _patch_bank(monkeypatch, JOKE)
    cog = _make_cog(online_members=["大肚"])

    await cog._fetch_dj_interjection_raw(_info())

    bank.match.assert_not_called()


@pytest.mark.asyncio
async def test_themed_lane_never_overridden_by_joke(monkeypatch):
    """themed 歌單已經有預先寫好的口白 → 不管冷卻/安靜與否都不換成笑話。"""
    import time
    bank = _patch_bank(monkeypatch, JOKE)
    cog = _make_cog(online_members=["大肚"])
    cog._last_dj_joke_ts = time.time() - 3600
    info = _info()
    info["_lane"] = "themed"
    info["_pick_reason"] = "主題歌單策展理由"

    dj = await cog._fetch_dj_interjection_raw(info)

    assert dj["text"] == "主題歌單策展理由"
    bank.match.assert_not_called()


@pytest.mark.asyncio
async def test_bypass_init_cog_never_fires_joke():
    """透過 __new__ 繞過 __init__ 建構的 cog（其他測試常見手法）沒有冷卻狀態，
    絕不能因此意外觸發笑話分支（也不能因為屬性不存在而炸掉）。"""
    from cogs.music_cog import MusicCog
    cog = MusicCog.__new__(MusicCog)
    assert not hasattr(cog, "_last_dj_joke_ts")
    assert getattr(cog, "_last_dj_joke_ts", None) is None
