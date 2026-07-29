"""TDD: DJ interjection 依 group size 注入語氣指令。

問題：_fetch_dj_interjection_raw 的 context 沒有 group size 感知，
     1 人時和 4+ 人時的語氣應該不同：
       1 人  → 親密聊天語氣，像對老朋友說話
       2-3 人 → 正常 DJ 語氣（無額外注入，不影響現有行為）
       4+ 人  → 精簡節奏，像 live DJ 播報

修法：_fetch_dj_interjection_raw 在 ctx 組裝尾端，讀 online members count
     並視 group size 附加一行語氣指令。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog(online_members: list[str]):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="DJ 語氣測試")
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
    cog._life_cores = MagicMock(return_value=[])
    cog._dj_topic_cooldown_store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))

    # mock vc.get_online_members
    fake_vc = MagicMock()
    fake_vc.get_online_members = MagicMock(return_value=online_members)
    cog._vc = MagicMock(return_value=fake_vc)
    return cog


def _info(requester="大肚"):
    return {"title": "夜曲", "uploader": "周杰倫", "requested_by": requester,
            "url": "https://example/x"}


def _ctx_str(cog) -> str:
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None
    return call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")


# ── 1. 1 人獨聽：親密語氣 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_listener_gets_intimate_tone_hint():
    """1 人在場 → context 含親密語氣提示。"""
    cog = _make_cog(online_members=["大肚"])
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "親密" in ctx or "一個人" in ctx, f"1人時應有親密語氣提示: {ctx!r}"


# ── 2. 2-3 人：正常，不加語氣行（不干擾現有行為）────────────────────────────

@pytest.mark.asyncio
async def test_small_group_no_tone_hint():
    """2-3 人 → 不注入特殊語氣行。"""
    cog = _make_cog(online_members=["大肚", "狗與露"])
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    # 不應有親密、也不應有 live DJ 大場面提示
    assert "親密" not in ctx
    assert "精簡" not in ctx and "節奏快" not in ctx


# ── 3. 4+ 人：live DJ 精簡節奏 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_large_group_gets_live_dj_tone_hint():
    """4+ 人 → context 含精簡節奏提示。"""
    cog = _make_cog(online_members=["大肚", "狗與露", "Alice", "Bob"])
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "精簡" in ctx or "節奏" in ctx or "短" in ctx, f"4+人時應有精簡語氣提示: {ctx!r}"


# ── 4. vc=None 時不崩潰（fail-open）────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_vc_does_not_crash():
    """vc() 回 None → 語氣注入靜默跳過，不影響 DJ 生成。"""
    cog = _make_cog(online_members=[])
    cog._vc = MagicMock(return_value=None)
    result = await cog._fetch_dj_interjection_raw(_info())
    assert result is not None
    assert result["text"]
