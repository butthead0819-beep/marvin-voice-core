"""
TDD：_detect_music_command 必須區分「強訊號 vs 弱訊號」play 關鍵字。

問題情境（5/16 23:20 log）：
- showay 說「馬文，播放控制」→ 命中 _MUSIC_PLAY_KW 的 "播放" substring
- 被當成 play song search="控制" → 真的去 YT 搜尋 → 播了「FBI解析《控制》」
- 使用者真實意圖不是點歌，是想看播放控制 UI

意圖層面的修法：
- 強訊號（"放音樂"、"放首歌"、"來首" 等含明確音樂字眼）→ 一律命中
- 弱訊號（"播放"、"我想聽"、"幫我找"、"放點" 等通用詞）→ 需通過 intent gate：
  - query 含 music intent marker（"的"、"歌"、"曲"、"音樂"、"MV" 等）→ 命中
  - 或弱訊號後續內容 ≥2 字 且不在 UI/系統詞 blocklist 內 → 命中
  - 否則 → 回 None，讓 Marvin LLM 用 context 判斷
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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
    cog.stt_logger = MagicMock()
    return cog


# ── 強訊號：含明確音樂字眼，一律命中 ──────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "放音樂",
    "播音樂",
    "放首歌",
    "播首歌",
    "放一首陶喆",
    "播一首",
    "來首老歌",
    "搜尋歌曲",
    "play music",
    "play song",
])
def test_strong_play_kw_always_matches(query):
    cog = _make_cog()
    assert cog._detect_music_command(query) == "play"


# ── 弱訊號 + music marker 命中 ───────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "播放陶喆的天天",       # 含「的」(possessive song pattern)
    "我想聽周杰倫的稻香",   # 含「的」
    "播放老歌",             # 含「歌」
    "幫我找一首抒情曲",     # 含「曲」+「一首」
    "放點輕音樂",           # 含「音樂」
    "幫我放陶喆的MV",       # 含「MV」+「的」
])
def test_weak_play_kw_with_music_marker_matches(query):
    cog = _make_cog()
    assert cog._detect_music_command(query) == "play"


# ── 弱訊號 + 無 marker 但後續內容看起來是名稱 → 命中 ────────────────────────

@pytest.mark.parametrize("query", [
    "播放周杰倫",  # 純人名，沒 marker，長度 ≥2
    "播放Adele",   # 英文人名
])
def test_weak_play_kw_artist_only_matches(query):
    """純人名（無「的」也無歌名 marker）但不在 blocklist 內，應該命中。

    這個 case 是最容易誤判的灰色地帶；目前選擇「寬鬆命中」，
    寧可送去 search 後讓 pick_best_music_candidate 判，也不要錯失點歌意圖。
    """
    cog = _make_cog()
    assert cog._detect_music_command(query) == "play"


# ── 弱訊號 + 後續是 UI/系統詞 → 不命中（核心 bug case） ────────────────────

@pytest.mark.parametrize("query", [
    "播放控制",     # ← 5/16 真實 log，誤判過的 case
    "播放清單",
    "播放列表",
    "播放設定",
    "播放選項",
    "播放畫面",
    "播放頁面",
    "播放音量",
    "播放狀態",
])
def test_weak_play_kw_blocked_by_ui_term(query):
    """弱訊號後面只跟著 UI/系統詞 → 不視為音樂意圖，回 None 讓 LLM 判。"""
    cog = _make_cog()
    assert cog._detect_music_command(query) is None, \
        f"'{query}' 不該被當成 music play 指令"


# ── 弱訊號 + 過短/空內容 → 不命中 ───────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "播放",          # 單獨命令詞，沒任何內容
    "播放。",        # 後面只有標點
    "我想聽",        # 弱訊號，沒後續
    "幫我找",        # 弱訊號，沒後續
])
def test_weak_play_kw_empty_target_returns_none(query):
    cog = _make_cog()
    assert cog._detect_music_command(query) is None


# ── 其他控制動作不受影響（既有行為） ───────────────────────────────────────

def test_skip_command_unchanged():
    cog = _make_cog()
    assert cog._detect_music_command("換一首") == "skip"
    assert cog._detect_music_command("下一首") == "skip"
    assert cog._detect_music_command("跳過這首") == "skip"


def test_stop_command_unchanged():
    cog = _make_cog()
    assert cog._detect_music_command("停止播放") == "stop"
    assert cog._detect_music_command("音樂停") == "stop"


def test_pause_command_unchanged():
    cog = _make_cog()
    assert cog._detect_music_command("暫停音樂") == "pause"
    assert cog._detect_music_command("暫停一下") == "pause"


def test_resume_command_unchanged():
    cog = _make_cog()
    assert cog._detect_music_command("繼續播") == "resume"
    assert cog._detect_music_command("繼續音樂") == "resume"


# ── _detect_music_direct_command 同步修正（同 bug 在 IBA-T0 路徑） ─────────

def test_direct_command_blocks_ui_term_in_play():
    """_detect_music_direct_command (IBA-T0, 不需喚醒詞) 也有同樣 bug。"""
    cog = _make_cog()
    assert cog._detect_music_direct_command("播放控制") is None


def test_direct_command_accepts_strong_play():
    cog = _make_cog()
    result = cog._detect_music_direct_command("我想聽陶喆的天天")  # weak kw + "的" marker
    assert result is not None
    assert result["action"] == "play"


def test_direct_command_skip_unchanged():
    cog = _make_cog()
    result = cog._detect_music_direct_command("下一首")
    assert result is not None
    assert result["action"] == "skip"
