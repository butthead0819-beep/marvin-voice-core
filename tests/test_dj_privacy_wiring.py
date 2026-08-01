"""TDD: present_speakers 從 _life_cores_async / _life_cores 接通到 recent_life_cores。

問題：
  1. _life_cores() 沒傳 present_speakers → privacy filter 永遠不觸發
  2. _life_cores_async() 沒讀 online members → 即使傳也是 None

修法：
  - _life_cores(entries, now, present_speakers=None) 傳給 recent_life_cores
  - _life_cores_async() 從 vc.get_online_members() 取在場人後傳入
  - _is_privacy_safe: 敏感 entry 的 participants 若未設，退用 entry.speakers

DiaryEntry.speakers 就是「這段對話的參與者」，對 is_sensitive=True 的 entry 用它做子集判斷。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helper ────────────────────────────────────────────────────────────────────

def _make_cog(online_members: list[str] | None = None, life_entries=None):
    """回傳 MusicCog，_load_summary_entries 注入 life_entries（避免讀真實檔案）。"""
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="DJ text")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="k")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    from dj_topic_selector import TopicCooldownStore
    import tempfile
    cog = MusicCog(bot)
    cog._dj_topic_cooldown_store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))

    # mock vc
    fake_vc = MagicMock()
    fake_vc.get_online_members = MagicMock(return_value=online_members or [])
    cog._vc = MagicMock(return_value=fake_vc)

    # 注入 diary entries（避免讀磁碟）
    cog._load_summary_entries = MagicMock(return_value=life_entries or [])

    return cog


def _entry(core: str, salience: str = "中", speakers=None, is_sensitive: bool = False):
    """模擬 DiaryEntry（有 speakers 但無 participants / is_sensitive）。"""
    import datetime as _dt
    e = MagicMock()
    e.ts_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    e.core = core
    e.salience = salience
    e.speakers = speakers or []
    # 模擬未來加的欄位：目前 DiaryEntry 沒有這兩個
    e.is_sensitive = is_sensitive
    e.participants = None  # DiaryEntry 沒有 participants，靠 speakers 兜底
    return e


def _info(requester="大肚"):
    return {"title": "夜曲", "uploader": "周杰倫", "requested_by": requester,
            "url": "https://example/x"}


# ── 1. _life_cores() 把 present_speakers 傳進 recent_life_cores ──────────────

def test_life_cores_passes_present_speakers_to_recent_life_cores():
    """_life_cores 有 present_speakers 參數，傳給 recent_life_cores。"""
    from cogs.music_cog import MusicCog
    import tempfile
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    cog = MusicCog(bot)

    with patch("dj_life_context.recent_life_cores_with_speakers") as mock_rlc:
        mock_rlc.return_value = []
        cog._life_cores([], now=1.0, present_speakers={"大肚"})
        _, kwargs = mock_rlc.call_args
        assert "present_speakers" in kwargs
        assert kwargs["present_speakers"] == {"大肚"}


# ── 2. _life_cores_async() 自動抓 online_members 並傳入 ─────────────────────

@pytest.mark.asyncio
async def test_life_cores_async_passes_online_members_as_present_speakers():
    """_life_cores_async 讀 vc.get_online_members()，傳給 _life_cores 的 present_speakers。"""
    cog = _make_cog(online_members=["大肚", "狗與露"])

    with patch.object(cog, "_life_cores") as mock_lc:
        mock_lc.return_value = []
        await cog._life_cores_async()
        mock_lc.assert_called_once()
        _, kwargs = mock_lc.call_args
        assert "present_speakers" in kwargs
        assert kwargs["present_speakers"] == {"大肚", "狗與露"}


@pytest.mark.asyncio
async def test_life_cores_async_passes_none_when_vc_unavailable():
    """vc() = None → present_speakers=None（不過濾，fail-open）。"""
    cog = _make_cog()
    cog._vc = MagicMock(return_value=None)

    with patch.object(cog, "_life_cores") as mock_lc:
        mock_lc.return_value = []
        await cog._life_cores_async()
        _, kwargs = mock_lc.call_args
        # vc 不可用時傳 None（不過濾）
        assert kwargs.get("present_speakers") is None


# ── 3. 端到端：敏感 entry 透過 _life_cores_async 被過濾 ─────────────────────

@pytest.mark.asyncio
async def test_sensitive_entry_filtered_e2e_via_life_cores_async():
    """敏感 entry，speakers 有人不在場 → 不進 LLM context。

    路徑：_life_cores_async → _life_cores → recent_life_cores → _is_privacy_safe
    """
    sensitive = _entry("大肚跟狗與露的秘密", is_sensitive=True,
                       speakers=["大肚", "狗與露"])
    public = _entry("今天天氣不錯")

    # 現場只有大肚，狗與露不在
    cog = _make_cog(online_members=["大肚"], life_entries=[sensitive, public])

    await cog._fetch_dj_interjection_raw(_info())
    ctx = cog.bot.router.generate_dynamic_system_msg.call_args
    ctx_str = ctx.kwargs.get("context", "") if ctx else ""
    assert "大肚跟狗與露的秘密" not in ctx_str, "敏感+不完整參與者 → 應被過濾"
    # 公開事件不受影響
    # （公開事件 is_sensitive=False，speakers 不做子集檢查，所以可能會或不會出現，
    #  這個測試只要確認敏感的被過濾掉就好）


@pytest.mark.asyncio
async def test_sensitive_entry_kept_when_all_speakers_present():
    """敏感 entry，speakers 全在場 → 保留進 context。"""
    sensitive = _entry("大肚跟狗與露的秘密", is_sensitive=True,
                       speakers=["大肚", "狗與露"])
    cog = _make_cog(online_members=["大肚", "狗與露"], life_entries=[sensitive])

    await cog._fetch_dj_interjection_raw(_info())
    ctx = cog.bot.router.generate_dynamic_system_msg.call_args
    ctx_str = ctx.kwargs.get("context", "") if ctx else ""
    assert "大肚跟狗與露的秘密" in ctx_str, "敏感但全員在場 → 應保留"


# ── 4. _is_privacy_safe：speakers 作為 sensitive entry 的 participants 兜底 ──

def test_is_privacy_safe_uses_speakers_as_fallback_for_sensitive():
    """is_sensitive=True + participants=None → 退用 entry.speakers 做子集判斷。"""
    from dj_life_context import _is_privacy_safe

    entry = MagicMock()
    entry.is_sensitive = True
    entry.participants = None          # 未設 participants
    entry.speakers = ["大肚", "狗與露"]  # 但有 speakers

    # 狗與露不在場 → False
    assert not _is_privacy_safe(entry, {"大肚"})
    # 兩人都在場 → True
    assert _is_privacy_safe(entry, {"大肚", "狗與露"})


def test_is_privacy_safe_non_sensitive_ignores_speakers():
    """is_sensitive=False → speakers 不影響判斷（公開事件，任何人都能聽）。"""
    from dj_life_context import _is_privacy_safe

    entry = MagicMock()
    entry.is_sensitive = False
    entry.participants = None
    entry.speakers = ["大肚", "狗與露"]

    # 只有 Alice 在場，但 entry 不敏感 → 仍通過
    assert _is_privacy_safe(entry, {"Alice"})
