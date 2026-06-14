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


@pytest.mark.parametrize("text", [
    "切歌",        # 6/14 incident：原本不在 IBA-T0 關鍵字表
    "我要切歌",    # 6/14 21:48:05 狗與露實況
])
def test_direct_command_skip_incident_20260614(text):
    cog = _make_cog()
    result = cog._detect_music_direct_command(text)
    assert result is not None, f"{text!r} 應觸發 skip"
    assert result["action"] == "skip"


# ── IBA-T0 長句不該誤觸發 (5/18 incident: "...直接跳過那個..." 被當 skip) ──

@pytest.mark.parametrize("text", [
    "讀寫資料庫的方式什麼的下什麼指令去讀取那個我都直接跳過那個他比我熟啊怎麼會比我瘦",  # 5/18 真實 case
    "我覺得我們可以下一首再說但是現在這個還沒講完真的不要這樣",
    "你之前說的那個事情我整個就跳過了沒有去處理",
    "我想聽你說完之後再決定要不要做這件事情",
    "我覺得停止播放這種事情根本不應該由我們決定吧你說對不對",
])
def test_direct_command_blocks_long_utterances(text):
    """IBA-T0 無喚醒詞觸發，長句 (>15 chars) 不該被 substring match 誤接。"""
    cog = _make_cog()
    assert cog._detect_music_direct_command(text) is None, \
        f"長句 ({len(text)} chars) '{text[:30]}...' 不該被當 IBA-T0 直達指令"


@pytest.mark.parametrize("text,expected", [
    ("跳過", "skip"),
    ("下一首", "skip"),
    ("換一首", "skip"),
    ("停止播放", "stop"),
    ("暫停音樂", "pause"),
    ("繼續播", "resume"),
    ("放點輕音樂", "play"),
])
def test_direct_command_short_utterance_still_works(text, expected):
    """短句 (≤15 chars) IBA-T0 直達照常運作。"""
    cog = _make_cog()
    result = cog._detect_music_direct_command(text)
    assert result is not None
    assert result["action"] == expected


# ── Gap A (2026-06-04)：長句夾帶明確「播放+具體歌名」應擷取救援 ──────────────
# 背景：陳進文「這樣妹妹說 曉雯幫我播放，孫淑媚的愛人」(~17 chars) >15 被長度閘整句
# 拒絕，明確點歌命令丟失（狗與露最後手動點）。長句裡若有「播放/我想聽 + 含 music
# marker 的具體目標」就擷取命令段。只救 play；control 詞長句一律不救（5/18 守門）。

@pytest.mark.parametrize("text,expect_target", [
    ("這樣妹妹說曉雯幫我播放孫淑媚的愛人", "孫淑媚的愛人"),       # 陳進文真實 case
    ("欸不是啦我剛剛想到幫我播放周杰倫的稻香", "周杰倫的稻香"),
    ("對啊對啊我覺得這個不錯我想聽五月天的溫柔",   "五月天的溫柔"),
])
def test_embedded_play_in_long_sentence_rescued(text, expect_target):
    cog = _make_cog()
    result = cog._detect_music_direct_command(text)
    assert result is not None, f"'{text}' 應擷取播放命令救援"
    assert result["action"] == "play"
    assert expect_target in result["query"]


@pytest.mark.parametrize("text", [
    "我想聽你說完之後再決定要不要做這件事情",          # 弱訊號但 tail 無 music marker
    "我覺得停止播放這種事情根本不應該由我們決定吧你說對不對",  # 含播放但 tail 無 marker
    "你之前說的那個事情我整個就跳過了沒有去處理",        # 無 play kw（只有 skip）
])
def test_embedded_play_without_music_marker_blocked(text):
    """長句救援嚴格守門：tail 無明確 music marker（的/歌/曲…）→ 不救，避 5/18 誤觸。"""
    cog = _make_cog()
    assert cog._detect_music_direct_command(text) is None
