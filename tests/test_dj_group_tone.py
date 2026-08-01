"""TDD: DJ interjection 依 group size 調整語氣。

問題：_fetch_dj_interjection_raw 的 context 沒有 group size 感知，
     1 人時和 4+ 人時的語氣應該不同：
       1 人  → 親密聊天語氣，像對老朋友說話
       2-3 人 → 正常 DJ 語氣（無額外注入，不影響現有行為）
       4+ 人  → 精簡節奏，像 live DJ 播報

修法：_fetch_dj_interjection_raw 在 ctx 組裝尾端，讀 online members count
     並視 group size 附加一行語氣指令（LLM 會被呼叫的情境）。

這幾個測試預設沒有 life/interest/對話/上一首 可用 → fallback 候選只剩
atmosphere/quick 輪替。atmosphere 永遠可用，全新 store 第一次會先選到它
（走 LLM），所以要測 quick 本地模板的三個測試，先把 store 的 last_fallback
標成 "atmosphere"，逼下一次輪到 quick——語氣改成看 _quick_segue_text 選中
的模板池，不是看 ctx 字串（quick 模式下 LLM 根本沒被呼叫，ctx 不會送出去）。
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


# ── 1. 1 人獨聽：親密語氣（無素材 → quick 本地模板，看選中的池）───────────────

@pytest.mark.asyncio
async def test_single_listener_gets_intimate_tone_hint():
    """1 人在場、沒有任何話題素材 → quick 模式挑親密語氣模板，不呼叫 LLM。"""
    from cogs.music_cog import MusicCog
    cog = _make_cog(online_members=["大肚"])
    cog._dj_topic_cooldown_store.set_last_fallback("atmosphere")
    dj = await cog._fetch_dj_interjection_raw(_info())
    cog.bot.router.generate_dynamic_system_msg.assert_not_awaited()
    assert dj["text"] in MusicCog._QUICK_SEGUE_TEMPLATES_INTIMATE


# ── 2. 2-3 人：正常池（不干擾現有行為）──────────────────────────────────────

@pytest.mark.asyncio
async def test_small_group_no_tone_hint():
    """2-3 人、沒有話題素材 → quick 模式用一般模板池，不是親密/精簡池。"""
    from cogs.music_cog import MusicCog
    cog = _make_cog(online_members=["大肚", "狗與露"])
    cog._dj_topic_cooldown_store.set_last_fallback("atmosphere")
    dj = await cog._fetch_dj_interjection_raw(_info())
    assert dj["text"] in MusicCog._QUICK_SEGUE_TEMPLATES
    assert dj["text"] not in MusicCog._QUICK_SEGUE_TEMPLATES_INTIMATE
    assert dj["text"] not in MusicCog._QUICK_SEGUE_TEMPLATES_ENERGETIC


# ── 3. 4+ 人：live DJ 精簡節奏 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_large_group_gets_live_dj_tone_hint():
    """4+ 人、沒有話題素材 → quick 模式挑精簡/帶勁模板。"""
    from cogs.music_cog import MusicCog
    cog = _make_cog(online_members=["大肚", "狗與露", "Alice", "Bob"])
    cog._dj_topic_cooldown_store.set_last_fallback("atmosphere")
    dj = await cog._fetch_dj_interjection_raw(_info())
    assert dj["text"] in MusicCog._QUICK_SEGUE_TEMPLATES_ENERGETIC


# ── 5. 有話題素材時，group-size 語氣行仍照舊送進 LLM context ────────────────

@pytest.mark.asyncio
async def test_tone_hint_still_reaches_llm_when_topic_available():
    """有素材（走 LLM 路徑）時，group-size 語氣提示應該還是照舊塞進 context。"""
    cog = _make_cog(online_members=["大肚", "狗與露", "Alice", "Bob"])
    cog._life_cores = MagicMock(return_value=["昨天去爬山"])
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
