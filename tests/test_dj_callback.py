"""TDD — E3: 舊事追問進 DJ 串場的扭蛋池（2026-10-02 Jack 拍板）。

1. select_mode: callbacks 可進入扭蛋池，抽中回傳 (topic, "callback")，並 mark_used；冷卻中不進池。
2. _present_callbacks: 只取在場者、只取 life=True、每人最多最舊 1 筆、present_members 空回空、例外 graceful degradation。
3. _fetch_dj_interjection_raw: mode=="callback" 時 context 包含舊事提示、consume_callback 被精確呼叫。
"""
from __future__ import annotations

import random
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cogs.music_cog import MusicCog
from dj_topic_selector import TopicCooldownStore, select_mode


def _fresh_store(tmp_path):
    return TopicCooldownStore(path=str(tmp_path / "dj_cd.json"))


def test_select_mode_callback_pool_and_cooldown(tmp_path, monkeypatch):
    """callbacks 進入扭蛋池，被抽中時 mark_used，冷卻中則不進池。"""
    store = _fresh_store(tmp_path)
    store.set_last_fallback("atmosphere")
    rng = random.Random(42)

    # 扭蛋池：只有 callback 與 atmosphere（quick 墊底被移除，last_fallback 排除 atmosphere）
    topic, mode = select_mode(
        [], [], store,
        callbacks=["A 之前說要買叉子"],
        rng=rng,
    )
    assert mode == "callback"
    assert topic == "A 之前說要買叉子"
    assert not store.is_cool("A 之前說要買叉子"), "抽中話題必須標記冷卻"

    # 冷卻中不再進池
    store.set_last_fallback("atmosphere")
    topic2, mode2 = select_mode(
        [], [], store,
        callbacks=["A 之前說要買叉子"],
        rng=rng,
    )
    assert mode2 != "callback"


def test_present_callbacks_filtering():
    """_present_callbacks 規則：只在場、只 life=True、每人最多最舊 1 筆、異常容錯。"""
    bot = MagicMock()
    cog = MusicCog(bot)

    # 1. present_members 為空或 None
    assert cog._present_callbacks(None) == ([], {})
    assert cog._present_callbacks([]) == ([], {})

    # 2. 正常在場過濾
    def fake_peek(user):
        if user == "Alice":
            return [
                {"text": "做伏地挺身", "life": True, "ts": 100},
                {"text": "學日文", "life": True, "ts": 200},
                {"text": "打副本", "life": False, "ts": 50},
            ]
        elif user == "Bob":
            return [{"text": "挖礦", "life": False, "ts": 300}]
        elif user == "Charlie":
            return [{"text": "出門跑步", "life": True, "ts": 400}]
        return []

    bot.router.memory.peek_all_shareable_callbacks.side_effect = fake_peek

    lines, src = cog._present_callbacks(["Bob", "Alice"])
    assert lines == ["Alice 之前說要做伏地挺身"]
    assert "Alice 之前說要做伏地挺身" in src
    assert src["Alice 之前說要做伏地挺身"] == ("Alice", {"text": "做伏地挺身", "life": True, "ts": 100})

    # 3. memory 拋例外容錯
    bot.router.memory.peek_all_shareable_callbacks.side_effect = RuntimeError("DB locked")
    assert cog._present_callbacks(["Alice"]) == ([], {})


@pytest.mark.asyncio
async def test_dj_interjection_callback_context_and_consume(tmp_path):
    """mode=="callback" 時，ctx 注入關心句型，且抽中立即 consume_callback。"""
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="好久沒做伏地挺身了呢")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="午後")

    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "cd.json"))

    cb_item = {"text": "做伏地挺身", "life": True, "ts": 100}
    with patch.object(cog, "_present_callbacks", return_value=(["Alice 之前說要做伏地挺身"], {"Alice 之前說要做伏地挺身": ("Alice", cb_item)})), \
         patch.object(cog, "_dj_clean_name", return_value=("歌曲", "歌手")), \
         patch("dj_narration_orchestrator.choose_mode", return_value=("Alice 之前說要做伏地挺身", "callback")):
        
        info = {"title": "歌曲 - 歌手", "uploader": "歌手", "requested_by": "Alice", "url": "https://example/x"}
        await cog._fetch_dj_interjection_raw(info)

    # 驗證 context 包含素材與開場鉤子
    call = bot.router.generate_dynamic_system_msg.call_args
    assert call is not None
    ctx = call.kwargs.get("context", "")
    assert "【你熟悉他的生活】他之前說過要做的事：" in ctx
    assert "Alice 之前說要做伏地挺身" in ctx
    assert "開場鉤子：點名順口關心這件事後來怎麼樣了" in ctx

    # 驗證 consume_callback 被以 ("Alice", cb_item) 呼叫
    bot.router.memory.consume_callback.assert_called_once_with("Alice", cb_item)
