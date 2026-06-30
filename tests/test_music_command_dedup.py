"""
TDD：5/18 incident — _handle_voice_music_command 重複觸發 + yt-dlp Errno 11 deadlock。

雙路徑（IBA-T0 + bus + speculative prefetch）可能對同一 wake 同時觸發
music command。並發 yt-dlp 呼叫在 macOS 競爭內部 lock → Resource deadlock。

修法：
A. _resolve_yt_query 對 OSError errno=11 重試一次（多次重試容易雪崩）
B. _handle_voice_music_command 入口 5s dedup，per-speaker
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mc_cog():
    """_resolve_yt_query 的實作已移到 MusicCog，用 MC 直接測。"""
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.music_memory = None
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _make_cog():
    """_handle_voice_music_command 已移至 MusicCog；vc_mock 提供 VC 依賴。"""
    bot = MagicMock()
    bot.guilds = []
    bot.music_memory = None
    # 連線中的 discord vc，讓 cmd="play" 不會提早 return
    _discord_vc = MagicMock()
    _discord_vc.is_connected.return_value = True
    bot.voice_clients = [_discord_vc]

    vc_mock = MagicMock()
    placeholder = MagicMock()
    placeholder.edit = AsyncMock()
    placeholder.delete = AsyncMock()
    vc_mock.active_text_channel = AsyncMock()
    vc_mock.active_text_channel.send = AsyncMock(return_value=placeholder)
    vc_mock.stt_logger = MagicMock()
    vc_mock._play_ack = AsyncMock()
    vc_mock._extract_music_search_query = MagicMock(return_value="陶喆天天")
    vc_mock._mixer = None

    def _cogs_get(name):
        if name == 'VoiceController':
            return vc_mock
        return None

    bot.cogs.get.side_effect = _cogs_get

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.stream_mode = False
    cog.radio_mode = False
    cog.stream_queue = []
    cog.stream_history = []
    cog._vc_mock = vc_mock
    return cog


# ── B: dedup 入口 5s 防抖 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_music_cmd_dedup_blocks_second_call_within_5s():
    """同 speaker 5s 內第二次 music command 應被 silently skip。"""
    cog = _make_cog()
    cog._resolve_yt_query = AsyncMock(return_value={"title": "x", "url": "u", "duration": 100})
    cog._check_song_duplicate = MagicMock(return_value=True)  # 讓第一次提早 return

    await cog._handle_voice_music_command("Alice", "播放陶喆的天天", "play")
    first_call_count = cog._resolve_yt_query.call_count
    assert first_call_count == 1

    # 5s 內再來
    await cog._handle_voice_music_command("Alice", "播放陶喆的天天", "play")
    # _resolve_yt_query 不該被第二次呼叫（dedup 提早 return）
    assert cog._resolve_yt_query.call_count == first_call_count


@pytest.mark.asyncio
async def test_music_cmd_dedup_does_not_block_different_speaker():
    """不同 speaker 不互相 block。"""
    cog = _make_cog()
    cog._resolve_yt_query = AsyncMock(return_value={"title": "x", "url": "u", "duration": 100})
    cog._check_song_duplicate = MagicMock(return_value=True)

    await cog._handle_voice_music_command("Alice", "播放陶喆", "play")
    await cog._handle_voice_music_command("Bob", "播放陶喆", "play")
    assert cog._resolve_yt_query.call_count == 2


@pytest.mark.asyncio
async def test_music_cmd_dedup_allows_call_after_5s():
    """5s 過後同 speaker 點『不同歌』可再呼叫（同句重派改由 query-aware 30s 守衛擋）。"""
    cog = _make_cog()
    cog._resolve_yt_query = AsyncMock(return_value={"title": "x", "url": "u", "duration": 100})
    cog._check_song_duplicate = MagicMock(return_value=True)

    fake_now = [100.0]

    def _fake_time():
        return fake_now[0]

    with patch("cogs.music_cog.time.time", _fake_time):
        await cog._handle_voice_music_command("Alice", "播放陶喆", "play")
        fake_now[0] += 6.0  # 過 5s
        await cog._handle_voice_music_command("Alice", "播放周杰倫", "play")  # 不同歌
    assert cog._resolve_yt_query.call_count == 2


# ── 內容去重：用穩定 video-id，不用會變的暫時串流 url ──────────────────────
# 2026-06-29 incident：同一句經喚醒+無喚醒 re-dispatch 各解析一次，info['url'] 是
# yt-dlp 每次都重產的 googlevideo 串流網址（帶 expiry token）→ 兩次不同 → 舊 dedup 比
# url 永遠不等 → 同一首歌入隊兩首（狗與露手動刪一首）。改用 webpage_url 的 video-id。

def test_check_song_duplicate_matches_same_video_despite_different_stream_url():
    cog = _make_mc_cog()
    cog.stream_queue = [{
        "title": "李榮浩 ft. 張惠妹《對等關係》Official Music Video",
        "webpage_url": "https://www.youtube.com/watch?v=mQUek1GYfvs",
        "url": "https://rr5---sn-aaa.googlevideo.com/videoplayback?ex=AAA&expire=1",
    }]
    # 同一首歌第二次解析：同 video-id，但暫時串流 url 不同
    dup = cog._check_song_duplicate(
        url="https://rr3---sn-bbb.googlevideo.com/videoplayback?ex=BBB&expire=2",
        title="李榮浩 ft. 張惠妹《對等關係》Official Music Video",
        username="狗與露",
        webpage_url="https://www.youtube.com/watch?v=mQUek1GYfvs",
        check_history=False,
    )
    assert dup is True, "同 video-id 即使暫時串流 url 不同，也要判定為重複"


def test_check_song_duplicate_matches_same_title_different_upload():
    """同名變體（不同 video-id 的重傳/cover）也視為重複——歌名正規化層。"""
    cog = _make_mc_cog()
    cog.stream_queue = [{
        "title": "周杰倫 Jay Chou【晴天 Sunny Day】Official MV",
        "webpage_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "url": "https://rr5.googlevideo.com/x",
    }]
    dup = cog._check_song_duplicate(
        url="https://rr3.googlevideo.com/y",
        title="周杰倫 Jay Chou【晴天 Sunny Day】Official MV (Live)",  # 同名 + Live 後綴
        username="狗與露", webpage_url="https://www.youtube.com/watch?v=zzzzzzzzzzz",
        check_history=False,
    )
    assert dup is True, "同名變體（不同上傳/版本）也要判定為重複"


def test_check_song_duplicate_different_video_not_blocked():
    cog = _make_mc_cog()
    cog.stream_queue = [{
        "title": "A 歌", "webpage_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "url": "https://rr5.googlevideo.com/x",
    }]
    dup = cog._check_song_duplicate(
        url="https://rr5.googlevideo.com/y", title="B 歌",
        username="狗與露", webpage_url="https://www.youtube.com/watch?v=bbbbbbbbbbb",
        check_history=False,
    )
    assert dup is False, "不同 video-id 不該誤判為重複"


# ── 入口 query-aware 去重：同句重派（喚醒+無喚醒）即使超過 5s 也擋 ──────────
# 2026-06-29 對等關係 incident：同一句經喚醒+無喚醒 re-dispatch 相隔 13s（>5s 時間窗），
# 舊的 speaker+5s dedup 漏 → 兩次解析 → 同歌入隊兩首。改用 speaker+正規化點歌字串。

@pytest.mark.asyncio
async def test_same_query_blocked_beyond_5s_window(monkeypatch):
    cog = _make_cog()
    cog._resolve_yt_query = AsyncMock(return_value={"title": "x", "url": "u", "webpage_url": "w", "duration": 100})
    cog._check_song_duplicate = MagicMock(return_value=False)  # 隔離內容去重，純測入口
    fake = [100.0]
    monkeypatch.setattr("cogs.music_cog.time.time", lambda: fake[0])
    await cog._handle_voice_music_command("狗與露", "播放張惠妹的對等關係", "play")
    n1 = cog._resolve_yt_query.call_count
    fake[0] += 13.0  # incident 相隔 13s（超過 5s 時間窗）
    await cog._handle_voice_music_command("狗與露", "播放張惠妹的對等關係", "play")
    assert cog._resolve_yt_query.call_count == n1, "同句 13s 後重派應被擋、不再解析"


@pytest.mark.asyncio
async def test_play_verb_variants_treated_as_same_query(monkeypatch):
    """『播放X』與『播X』正規化後同一句，仍要擋。"""
    cog = _make_cog()
    cog._resolve_yt_query = AsyncMock(return_value={"title": "x", "url": "u", "webpage_url": "w", "duration": 100})
    cog._check_song_duplicate = MagicMock(return_value=False)
    fake = [100.0]
    monkeypatch.setattr("cogs.music_cog.time.time", lambda: fake[0])
    await cog._handle_voice_music_command("狗與露", "播放張惠妹的對等關係", "play")
    n1 = cog._resolve_yt_query.call_count
    fake[0] += 10.0
    await cog._handle_voice_music_command("狗與露", "播張惠妹的對等關係", "play")
    assert cog._resolve_yt_query.call_count == n1


@pytest.mark.asyncio
async def test_different_song_after_window_allowed(monkeypatch):
    """超過 5s 後點不同歌應放行（query-aware 只擋同一句）。"""
    cog = _make_cog()
    cog._resolve_yt_query = AsyncMock(return_value={"title": "x", "url": "u", "webpage_url": "w", "duration": 100})
    cog._check_song_duplicate = MagicMock(return_value=False)
    fake = [100.0]
    monkeypatch.setattr("cogs.music_cog.time.time", lambda: fake[0])
    await cog._handle_voice_music_command("狗與露", "播放張惠妹的對等關係", "play")
    n1 = cog._resolve_yt_query.call_count
    fake[0] += 13.0
    await cog._handle_voice_music_command("狗與露", "播放周杰倫的晴天", "play")  # 不同歌
    assert cog._resolve_yt_query.call_count == n1 + 1, "不同歌應放行"


# ── A: yt-dlp Errno 11 retry ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_yt_query_retries_on_errno_11():
    """OSError(11) 第一次 _extract 失敗 → 等 200ms 重試 → 第二次成功則返回。

    注意 _extract 內部 yt_dlp.YoutubeDL().extract_info 會被呼叫多次
    （ytmsearch5 + ytsearch5）。retry 機制在更上層的 await loop.run_in_executor
    所以是「整個 _extract 失敗才 retry」，不是個別 extract_info call。
    """
    cog = _make_mc_cog()

    extract_attempt = [0]  # 計算 _extract 被呼叫幾次（重試後 +1）
    music_entry = {"title": "Song", "url": "http://stream/x",
                    "uploader": "X", "categories": ["Music"],
                    "duration": 200, "webpage_url": "u", "thumbnail": "t"}

    class _FlakyYDL:
        def __init__(self, *a, **kw): pass
        def __enter__(self):
            extract_attempt[0] += 1
            return self
        def __exit__(self, *a): return False
        def extract_info(self, search, download=False):
            if extract_attempt[0] == 1:
                # 第一輪 _extract → 第一次 extract_info call 就 raise
                raise OSError(11, "Resource deadlock avoided")
            # 第二輪（重試）→ ytmsearch5 回正常結果
            return {"entries": [music_entry]}

    with patch("yt_dlp.YoutubeDL", _FlakyYDL):
        info = await cog._resolve_yt_query("陶喆的天天")

    assert info is not None
    assert info["title"] == "Song"
    assert extract_attempt[0] == 2  # 重試一次


@pytest.mark.asyncio
async def test_resolve_yt_query_returns_none_after_double_errno_11():
    """連續兩次 Errno 11 → 返回 None（不無限重試）。"""
    cog = _make_mc_cog()

    class _AlwaysDeadlock:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **kw):
            raise OSError(11, "Resource deadlock avoided")

    with patch("yt_dlp.YoutubeDL", _AlwaysDeadlock):
        info = await cog._resolve_yt_query("無解的歌")

    assert info is None


@pytest.mark.asyncio
async def test_safe_music_command_catches_exception_and_notifies_user():
    """top-level wrapper: 任何 exception 都被吞 + 通知 user。

    5/18 17:51 incident: Errno 11 從 _handle_voice_music_command 內部冒出
    但 retry 沒觸發 → 錯誤不在 yt-dlp 是更早的 code。需要 traceback + UX。
    """
    cog = _make_cog()
    cog._handle_voice_music_command = AsyncMock(side_effect=OSError(11, "Resource deadlock avoided"))

    # 不該 raise，內部吞掉
    await cog._safe_music_command("Alice", "播放陶喆", "play")

    # 應該已通知 user（_safe_music_command 透過 vc_mock.active_text_channel 貼訊息）
    ch = cog._vc_mock.active_text_channel
    ch.send.assert_called()
    sent_msg = ch.send.call_args[0][0]
    assert "音樂系統" in sent_msg or "出錯" in sent_msg
    assert "OSError" in sent_msg  # 顯示 exception type 方便 debug


@pytest.mark.asyncio
async def test_safe_music_command_passes_through_when_normal():
    """正常 case: handler 不 raise，wrapper 純透傳，不通知 user。"""
    cog = _make_cog()
    cog._handle_voice_music_command = AsyncMock(return_value=None)

    await cog._safe_music_command("Alice", "播放陶喆", "play")
    cog._handle_voice_music_command.assert_awaited_once_with("Alice", "播放陶喆", "play")
    # 沒 exception → 不該通知 user 出錯
    # (test channel mock 的 send 可能被 handler 內部呼叫過，無法直接 assert_not_called)


@pytest.mark.asyncio
async def test_resolve_yt_query_does_not_retry_on_other_oserror():
    """其他 OSError (非 errno=11) 不重試，直接返回 None。"""
    cog = _make_mc_cog()
    call_count = [0]

    class _OtherError:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **kw):
            call_count[0] += 1
            raise OSError(2, "No such file or directory")

    with patch("yt_dlp.YoutubeDL", _OtherError):
        info = await cog._resolve_yt_query("query")

    assert info is None
    assert call_count[0] == 1  # 只試一次
