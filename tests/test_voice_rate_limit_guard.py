"""☢️ [Voice Rate-Limit Guard] 官方語音關閉碼「不該重連」→ AutoRejoin 要退避，不能硬打。

2026-09-16 晚事故：Discord 對 bot 帳號連續丟 4021（RateLimited）／4006（SessionNoLongerValid），
AutoRejoin 每 60s 巡一次、discord.py 內部每輪又試 5 次 identify，等於在官方明講「不該重連」
的代碼上持續硬闖（4021 官方定義 May Reconnect=No）。討論見 2026-09-17 對話：先把官方代碼表
弄清楚，一次寫好對應行為——不是「換伺服器」「等它過」，是不再對這些代碼硬重連。

discord.py 2.7.1 已知怪癖（見 venv_simon/.../discord/voice_state.py `_inner_connect`）：
5 次 attempt 全部 ConnectionClosed 時 for 迴圈不 raise，`_connect()` 照印
"Voice connection complete." 再馬上以 1000 自我斷線——我們自己的 try/except 在
auto_rejoin_on_boot 抓不到例外，只能靠 discord.voice_state log 觀察器抓真正發生的代碼。
"""
from __future__ import annotations

import logging
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.voice_controller_connection import (
    VOICE_CLOSE_CODES,
    _VoiceCircuitBreakerObserver,
)


def _rec(msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        "discord.voice_state", logging.WARNING, __file__, 0, msg, None, exc_info
    )


class _FakeConnectionClosed(Exception):
    def __init__(self, code: int):
        super().__init__(f"Shard ID None WebSocket closed with {code}")
        self.code = code


def _exc_info(code: int):
    exc = _FakeConnectionClosed(code)
    return (type(exc), exc, None)


# ── 官方代碼表：4021 必須是 may_reconnect=False（本次事故的核心） ────────────

def test_4021_is_no_reconnect_per_official_docs():
    name, may_reconnect = VOICE_CLOSE_CODES[4021]
    assert name == "RateLimited"
    assert may_reconnect is False


def test_4006_is_no_reconnect_per_official_docs():
    name, may_reconnect = VOICE_CLOSE_CODES[4006]
    assert name == "SessionNoLongerValid"
    assert may_reconnect is False


def test_4015_may_reconnect_true_not_flagged():
    """4015（VoiceServerCrashed）官方建議 resume，discord.py 已自動處理，不該觸發冷卻。"""
    name, may_reconnect = VOICE_CLOSE_CODES[4015]
    assert name == "VoiceServerCrashed"
    assert may_reconnect is True


# ── _VoiceCircuitBreakerObserver：從 log 抓官方定義的「不該重連」代碼 ─────────

def test_observer_catches_4021_plain_warning_no_exc_info():
    """4021 的 log 是純文字警告（_poll_voice_ws 沒把代碼塞進訊息），要靠寫死比對認出來。"""
    hits = []
    obs = _VoiceCircuitBreakerObserver(lambda code: hits.append(code))
    obs.emit(_rec("We are being ratelimited while trying to connect to voice. Disconnecting..."))
    assert hits == [4021]


def test_observer_catches_4006_via_exc_info_code_attr():
    """4006 走 _log.exception(...)，exc_info[1] 是 ConnectionClosed，直接讀 .code。"""
    hits = []
    obs = _VoiceCircuitBreakerObserver(lambda code: hits.append(code))
    obs.emit(_rec("Failed to connect to voice... Retrying in 1.0s...", exc_info=_exc_info(4006)))
    assert hits == [4006]


def test_observer_ignores_reconnectable_code_4015():
    hits = []
    obs = _VoiceCircuitBreakerObserver(lambda code: hits.append(code))
    obs.emit(_rec("Disconnected from voice, attempting a resume...", exc_info=_exc_info(4015)))
    assert hits == []


def test_observer_ignores_normal_closure_1000():
    hits = []
    obs = _VoiceCircuitBreakerObserver(lambda code: hits.append(code))
    obs.emit(_rec("Disconnecting from voice normally, close code 1000.", exc_info=_exc_info(1000)))
    assert hits == []


def test_observer_ignores_message_with_no_code_signal():
    hits = []
    obs = _VoiceCircuitBreakerObserver(lambda code: hits.append(code))
    obs.emit(_rec("Voice connection complete."))
    assert hits == []


# ── _record_voice_no_reconnect_code：指數退避 + 120s 穩定歸零 ───────────────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None
    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.self_restart = AsyncMock()
    return cog


def test_first_violation_sets_base_backoff():
    cog = _make_cog()
    assert cog._voice_cooldown_until == 0.0
    cog._record_voice_no_reconnect_code(4021)
    assert cog._voice_backoff_s == pytest.approx(300.0)
    assert cog._voice_cooldown_until > time.time()


def test_repeat_violation_without_stable_connection_doubles_backoff():
    cog = _make_cog()
    cog._record_voice_no_reconnect_code(4021)
    first = cog._voice_backoff_s
    cog._record_voice_no_reconnect_code(4006)
    assert cog._voice_backoff_s == pytest.approx(first * 2)


def test_backoff_capped():
    cog = _make_cog()
    for _ in range(10):
        cog._record_voice_no_reconnect_code(4021)
    assert cog._voice_backoff_s <= 3600.0


def test_backoff_resets_after_stable_connection():
    cog = _make_cog()
    cog._record_voice_no_reconnect_code(4021)
    assert cog._voice_backoff_s > 0
    # 連線曾穩定超過 120s（鏡像 soft_repair_count 的歸零門檻）
    cog.connection_time = time.time() - 200
    cog._record_voice_no_reconnect_code(4006)
    assert cog._voice_backoff_s == pytest.approx(300.0)  # 回到 base，不是疊加


# ── auto_rejoin_on_boot：冷卻中要 skip，不能連 pick_rejoin_channel 都不查就硬打 ──

@pytest.mark.asyncio
async def test_auto_rejoin_skips_during_cooldown():
    cog = _make_cog()
    cog._voice_cooldown_until = time.time() + 300
    # ⚠️ main_discord.py import 會 load_dotenv() 讀進 repo 現有 .env（可能含臨時
    # MARVIN_AUTO_REJOIN=0），在同一個 pytest 進程裡污染後面的測試。這裡要測的是
    # 冷卻邏輯本身，跟 .env 現狀無關，用 patch.dict 明確蓋掉、不依賴環境。
    with patch.dict(os.environ, {"MARVIN_AUTO_REJOIN": "1"}), \
         patch("cogs.voice_controller_connection.pick_rejoin_channel") as mock_pick:
        await cog.auto_rejoin_on_boot()
        mock_pick.assert_not_called()


@pytest.mark.asyncio
async def test_auto_rejoin_proceeds_after_cooldown_expires():
    cog = _make_cog()
    cog._voice_cooldown_until = time.time() - 1  # 已過期
    with patch.dict(os.environ, {"MARVIN_AUTO_REJOIN": "1"}), \
         patch("cogs.voice_controller_connection.pick_rejoin_channel", return_value=None) as mock_pick:
        await cog.auto_rejoin_on_boot()
        mock_pick.assert_called_once()


def test_install_circuit_breaker_watch_is_idempotent():
    cog = _make_cog()
    lg = logging.getLogger("discord.voice_state")
    before = list(lg.handlers)
    try:
        cog._install_voice_flap_watch()
        cog._install_voice_flap_watch()
        added = [h for h in lg.handlers if isinstance(h, _VoiceCircuitBreakerObserver)]
        assert len(added) == 1
    finally:
        for h in list(lg.handlers):
            if isinstance(h, (_VoiceCircuitBreakerObserver,)) or type(h).__name__ == "_VoiceFlapObserver":
                lg.removeHandler(h)
        assert list(lg.handlers) == before


# ── auto_rejoin_on_boot 防重入：多個觸發源（on_ready / sentinel 60s tick）沒有互斥鎖，
# 2026-09-17 15:xx 事故實測 log 出現 4 行近乎同一毫秒的重複 cooldown-skip 訊息，代表
# 曾經有多個 auto_rejoin_on_boot() 併發在跑。單一 tasks.loop 本身不會自我重疊（discord.py
# 內部序列化），但 on_ready 用 asyncio.create_task 沒等待、也沒鎖，一旦跟 sentinel tick
# 或彼此重疊執行，就可能對同一頻道發出多個並行 identify——這正是 Discord 判定濫用/觸發
# 4021 的合理成因。修法：整段（含 pick_rejoin_channel 到 connect 完成）用旗標防重入。

def test_auto_rejoin_reentrancy_guard_blocks_concurrent_call():
    cog = _make_cog()
    cog._auto_rejoin_running = True  # 模拟已经有一个在跑
    with patch("cogs.voice_controller_connection.pick_rejoin_channel") as mock_pick:
        import asyncio
        asyncio.run(cog.auto_rejoin_on_boot())
        mock_pick.assert_not_called()


@pytest.mark.asyncio
async def test_auto_rejoin_sets_and_clears_running_flag_on_success():
    cog = _make_cog()
    ch = MagicMock()
    ch.members = [MagicMock(bot=False)]
    with patch.dict(os.environ, {"MARVIN_AUTO_REJOIN": "1"}), \
         patch("cogs.voice_controller_connection.pick_rejoin_channel", return_value=None):
        assert cog._auto_rejoin_running is False
        await cog.auto_rejoin_on_boot()
        assert cog._auto_rejoin_running is False  # finally 清乾淨，不會卡死


@pytest.mark.asyncio
async def test_auto_rejoin_clears_running_flag_even_on_exception():
    cog = _make_cog()
    with patch.dict(os.environ, {"MARVIN_AUTO_REJOIN": "1"}), \
         patch("cogs.voice_controller_connection.pick_rejoin_channel", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            await cog.auto_rejoin_on_boot()
        assert cog._auto_rejoin_running is False
