"""TDD: _extract_music_search_query — noise prefix stripping.

5/18 22:00–23:54 live session 觀察到 STT 在喚醒詞前後保留語助詞 noise，
被原版 _extract_music_search_query 直接送進 yt-dlp 搜尋 → 選到錯歌：

  search='把我播放孫燕姿的開始了'      → 「開始懂了」（不是「開始了」）
  search='麻煩播放孫燕姿的天黑黑'      → 「浪流連」（完全錯）
                                          後再試一次才搜到「天黑黑」

原版 bug：t.startswith(prefix) 只比對句首；prefix 前若有 noise（麻煩/把我/
好煩/Marvin,）整段不剝離，直接帶 noise 進 yt-dlp。

修法：在 head 視窗（≤ NOISE_WINDOW chars）內掃最遠的 music kw end，
切掉「noise + kw」整段。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


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


# ── 基準：無 noise 不改變行為（regression guard）─────────────────────────

@pytest.mark.parametrize("query,expected", [
    ("播放陶喆的天天", "陶喆的天天"),
    ("我想聽周杰倫的稻香", "周杰倫的稻香"),
    ("放音樂", ""),
    ("放一首陶喆", "陶喆"),
    ("播一首周杰倫的歌", "周杰倫的歌"),
])
def test_baseline_no_noise_no_regression(query, expected):
    cog = _make_cog()
    assert cog._extract_music_search_query(query) == expected


# ── 核心修法：head 視窗內 noise prefix 必須被切掉 ────────────────────────

@pytest.mark.parametrize("query,expected", [
    # 5/18 22:00–23:54 真實 incident
    ("麻煩播放孫燕姿的天黑黑", "孫燕姿的天黑黑"),
    ("把我播放孫燕姿的開始了", "孫燕姿的開始了"),
    # 設計目標案例
    ("好煩，播放陶喆的天天", "陶喆的天天"),
    ("欸，播放陶喆的天天", "陶喆的天天"),
    ("嗯，我想聽周杰倫的稻香", "周杰倫的稻香"),
])
def test_noise_prefix_stripped_before_play_kw(query, expected):
    cog = _make_cog()
    assert cog._extract_music_search_query(query) == expected


# ── Wake-word 在中段（_strip_wake_word 不接）也要靠 noise window 救回 ──

def test_wake_word_in_middle_still_strippable():
    """'好煩，馬文，播放陶喆' — _strip_wake_word 因句首不是 wake 不剝；
    新邏輯靠 noise window 找到「播放」並剝乾淨。"""
    cog = _make_cog()
    # 馬文 + 標點 共 3 chars，加 "好煩，" 5 chars，"播放" 在 idx 6
    # NOISE_WINDOW 需 ≥ 6 才能救
    assert cog._extract_music_search_query("好煩，馬文，播放陶喆的天天") == "陶喆的天天"


# ── 邊界：noise 太長就放棄剝，避免歌名被切掉 ───────────────────────────

def test_kw_too_deep_in_text_not_stripped():
    """歌名中段才出現 kw（pathological），不該誤切。"""
    cog = _make_cog()
    # 「陶喆的天天就是要播放陶喆」— "播放" 在 idx 9 (>NOISE_WINDOW=8)
    # 保留原樣（_strip_wake_word 後）
    out = cog._extract_music_search_query("陶喆的天天就是要播放陶喆")
    # 不該切到 idx 11 後變成 "陶喆" — 應該保留整句
    assert "天天" in out


# ── 重疊 kw 取最晚結束的 ─────────────────────────────────────────────────

def test_overlapping_kws_take_furthest_end():
    """'麻煩我想聽播放陶喆' — '我想聽' 在 idx 2, '播放' 在 idx 5；
    取最遠 end（'播放' end=7）→ 剝到 '陶喆'。"""
    cog = _make_cog()
    assert cog._extract_music_search_query("麻煩我想聽播放陶喆") == "陶喆"


# ── 標點清理 ──────────────────────────────────────────────────────────────

def test_punctuation_after_kw_cleaned():
    cog = _make_cog()
    assert cog._extract_music_search_query("麻煩，播放，陶喆的天天") == "陶喆的天天"


# ── 後綴語助詞保留行為（不該被新邏輯影響）────────────────────────────

def test_trailing_particles_still_stripped():
    cog = _make_cog()
    assert cog._extract_music_search_query("麻煩播放陶喆的天天好嗎") == "陶喆的天天"
    assert cog._extract_music_search_query("播放陶喆的天天吧") == "陶喆的天天"
