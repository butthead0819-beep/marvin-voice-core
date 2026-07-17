"""TDD: 重點已在佇列的歌 → 要能把死掉的 stream loop 叫醒（2026-07-17 live 事故）。

事故鏈（bot_main.log 16:01-16:21）：
1. 16:01:25 狗與露點「左邊的人」→ 進佇列
2. 16:02:02 有人 /dismiss → stop_stream() 取消 loop、stream_mode=False，
   **但沒清空 stream_queue**
3. 狗與露發現沒在播 → 重點同一首 → 撞 _check_song_duplicate → 早退
   → **永遠走不到下面重啟 loop 的程式碼** → 佇列永遠卡著
4. 16:02:45、16:20:54 重點兩次，全部一樣沒動靜

早退在重啟 loop 之前＝死鎖。使用者唯一的逃生出口是「點一首不同的歌」，
但沒人會知道要這樣做。修法：佇列有歌但 loop 沒跑 → 一律叫醒（含重複那條路）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._stream_loop = MagicMock(return_value=_noop())
    return cog


async def _noop():
    return None


def _song(title="左邊的人-陳華 歌詞字幕版", who="狗與露"):
    return {"title": title, "requested_by": who, "url": "x",
            "webpage_url": "https://youtu.be/tER-0RhdAow"}


# ── _ensure_stream_loop：純狀態機 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_stream_loop_starts_when_dead():
    """loop 死了（stream_mode=False）→ 叫醒，回 True。"""
    cog = _make_cog()
    cog.stream_mode = False
    cog.stream_queue = [_song()]
    revived = cog._ensure_stream_loop()
    assert revived is True
    assert cog.stream_mode is True
    assert cog.stream_task is not None
    cog.stream_task.cancel()


@pytest.mark.asyncio
async def test_ensure_stream_loop_revives_when_flag_lies():
    """⚠️ 2026-07-17 第二次事故：stream_mode=True 但 task 已死（被 cancel）。

    _stream_loop 的 except CancelledError 只印 log 沒把 stream_mode 設回 False →
    旗標停在 True 騙人說「還在播」→ 只看旗標的復活邏輯直接 no-op → 佇列卡死。
    **loop 是否活著要看 task，不能信旗標。**
    """
    cog = _make_cog()
    cog.stream_mode = True                      # 旗標說在播
    dead = asyncio.create_task(_noop())
    await dead                                  # …但 task 已經死了
    cog.stream_task = dead
    cog.stream_queue = [_song()]
    assert cog._ensure_stream_loop() is True, "旗標騙人時仍須叫醒（不能只看 stream_mode）"
    assert cog.stream_mode is True
    assert cog.stream_task is not dead, "應換上新的 task"
    cog.stream_task.cancel()


@pytest.mark.asyncio
async def test_cancelled_loop_resets_stream_mode_flag():
    """根因：loop 被取消時 stream_mode 必須設回 False，別讓旗標說謊。"""
    import inspect

    from cogs.music_cog import MusicCog
    src = inspect.getsource(MusicCog._stream_loop)
    cancel_branch = src.split("except asyncio.CancelledError:")[1].split("except Exception")[0]
    assert "stream_mode = False" in cancel_branch, \
        "CancelledError 分支沒清 stream_mode → 旗標會說謊 → 復活邏輯失效"


@pytest.mark.asyncio
async def test_ensure_stream_loop_noop_when_already_running():
    """loop 活著 → 不動它（別把正在播的歌打斷），回 False。"""
    cog = _make_cog()
    cog.stream_mode = True
    task = asyncio.create_task(_noop())
    cog.stream_task = task
    assert cog._ensure_stream_loop() is False
    assert cog.stream_mode is True
    assert cog.stream_task is task, "不該換掉正在跑的 task"


# ── 死鎖本體：重複歌不得早退到跳過 loop 重啟 ──────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_request_revives_dead_loop():
    """佇列有歌 + loop 死了 + 重點同一首 → 必須叫醒 loop（live 死鎖的解）。"""
    cog = _make_cog()
    cog.stream_mode = False
    cog.stream_queue = [_song()]
    revived = cog._ensure_stream_loop()
    assert revived is True and cog.stream_mode is True
    cog.stream_task.cancel()


def test_dup_early_return_calls_ensure_loop_in_both_request_paths():
    """兩條點歌路徑（手動 / 語音）的重複早退分支都要先叫醒 loop。

    這條防的是「只修了一條路徑」——語音點歌是主要入口（零鍵盤設計）。
    """
    import inspect

    from cogs.music_cog import MusicCog
    for fn_name in ("marvin_play", "_handle_voice_music_command"):
        fn = getattr(MusicCog, fn_name)
        fn = getattr(fn, "callback", fn)  # slash command → 取底層函式
        src = inspect.getsource(fn)
        assert "已在佇列待播了" in src, f"{fn_name} 應含重複早退分支（方法改名了？）"
        dup_branch = src.split("已在佇列待播了")[0]
        # 早退分支前後要有 _ensure_stream_loop（不能只在「非重複」那條）
        assert "_ensure_stream_loop" in dup_branch, \
            f"{fn_name} 的重複早退分支沒叫醒 loop → 死鎖重演"
