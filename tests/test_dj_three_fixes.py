"""10/18 DJ 口白三個小修：
A. song_name_clean：YouTube「XX - Topic」頻道名不要被當歌手照念（Release 例外）。
B. dj_heat.usable_conv_text + music_cog_dj_lyrics 入口過濾：STT 糊字/指令/網址不進 DJ 對話素材；
   dj_narration_orchestrator 對話素材 prompt 加「不准照抄原句」規則。
C. quick 罐頭句擴充三池 + 帶歌名 + 不連續重複。
"""
from __future__ import annotations

import time

import pytest

from song_name_clean import _artist_from_uploader, dj_display_name
from dj_heat import usable_conv_text
from dj_narration_orchestrator import DJMaterials, _render_conversation, _render_perspective

from tests.test_dj_group_tone import _make_cog, _info, _no_quick


# ── A: song_name_clean ──────────────────────────────────────────────────────

def test_artist_from_uploader_strips_topic_suffix():
    assert _artist_from_uploader("Release - Topic") == ""
    assert _artist_from_uploader("Mayday - Topic") == "Mayday"
    assert _artist_from_uploader("李昕融 - Topic") == "李昕融"
    assert _artist_from_uploader("周杰倫") == "周杰倫"
    assert _artist_from_uploader("") == ""


def test_dj_display_name_drops_release_topic():
    title, artist = dj_display_name(
        {"title": "天使的眼淚", "uploader": "Release - Topic"},
        extract_vid=lambda s: None,
        catalog_index={},
    )
    assert (title, artist) == ("天使的眼淚", "")


def test_dj_display_name_keeps_real_artist_topic():
    title, artist = dj_display_name(
        {"title": "突然好想你", "uploader": "Mayday - Topic"},
        extract_vid=lambda s: None,
        catalog_index={},
    )
    assert (title, artist) == ("突然好想你", "Mayday")


# ── B1: dj_heat.usable_conv_text ─────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "o see so don notet's come",
    "Play",
    "565664 桶",
    "馬文播放 bak Street boys的 I w",
    "放一首https://www.youtube.co",
    "",
])
def test_usable_conv_text_false(text):
    assert usable_conv_text(text) is False


@pytest.mark.parametrize("text", [
    "不要吃太硬的東西",
    "你說 now playing 然後是有圖片的那一頁",
    "那個不是 15 年的那一個",
])
def test_usable_conv_text_true(text):
    assert usable_conv_text(text) is True


# ── B3: dj_narration_orchestrator conv prompt rule ──────────────────────────

def test_render_conversation_has_no_copy_rule():
    m = DJMaterials(conv_lines=["狗與露：「今天好累」"])
    out = "\n".join(_render_conversation(None, m))
    assert "不准照抄原句" in out
    assert "語音辨識稿" in out


def test_render_perspective_has_no_copy_rule():
    m = DJMaterials(conv_lines=["狗與露：「今天好累」"])
    out = "\n".join(_render_perspective(None, m))
    assert "不准照抄原句" in out
    assert "語音辨識稿" in out


# ── C: quick 罐頭句 ───────────────────────────────────────────────────────────

def test_quick_segue_template_pools_disjoint():
    from cogs.music_cog import MusicCog
    default_pool = set(MusicCog._QUICK_SEGUE_TEMPLATES)
    intimate_pool = set(MusicCog._QUICK_SEGUE_TEMPLATES_INTIMATE)
    energetic_pool = set(MusicCog._QUICK_SEGUE_TEMPLATES_ENERGETIC)
    assert default_pool & intimate_pool == set()
    assert default_pool & energetic_pool == set()
    assert intimate_pool & energetic_pool == set()


def test_quick_segue_template_excludes_recent():
    from cogs.music_cog import MusicCog
    pool = MusicCog._QUICK_SEGUE_TEMPLATES
    recent = tuple(pool[1:])
    for _ in range(20):
        assert MusicCog._quick_segue_template(n_online=2, recent=recent) == pool[0]


def test_quick_segue_template_recent_covers_whole_pool():
    from cogs.music_cog import MusicCog
    pool = MusicCog._QUICK_SEGUE_TEMPLATES
    for _ in range(20):
        result = MusicCog._quick_segue_template(n_online=2, recent=tuple(pool))
        assert result in pool


def test_quick_song_label():
    from cogs.music_cog import MusicCog
    assert MusicCog._quick_song_label("夜曲", "周杰倫") == "周杰倫的《夜曲》"
    assert MusicCog._quick_song_label("夜曲", "") == "《夜曲》"


# ── 接線測試：quick 模式真的代入歌名 + 不連續重複 ────────────────────────────

@pytest.mark.asyncio
async def test_quick_mode_fills_song_name_and_avoids_immediate_repeat():
    """最近用過的模板不選：池只剩一句沒用過 → 必選它，且 {song} 代入歌名、記進 recent。"""
    from cogs.music_cog import MusicCog
    cog = _make_cog(online_members=["大肚", "狗與露"])
    cog._dj_topic_cooldown_store.set_last_fallback("atmosphere")
    info = _info()
    pool = MusicCog._QUICK_SEGUE_TEMPLATES
    left = next(t for t in pool if "{song}" in t)
    cog._recent_quick_templates = [t for t in pool if t != left]

    dj = await cog._fetch_dj_interjection_raw(info)

    assert dj["text"] == left.format(song=MusicCog._quick_song_label(*cog._dj_clean_name(info)))
    assert "《" in dj["text"]
    assert cog._recent_quick_templates[-1] == left
    assert len(cog._recent_quick_templates) == 3


@pytest.mark.asyncio
async def test_dirty_stt_conv_line_excluded_from_llm_context(monkeypatch):
    from unittest.mock import MagicMock
    _no_quick(monkeypatch)
    cog = _make_cog(online_members=["大肚", "狗與露", "Alice", "Bob"])
    cog._life_cores = MagicMock(return_value=["昨天去爬山"])
    cog.bot.router.atmosphere_tracker = None
    cog.bot.engine.conv_buffer.get_last_n_utterances.return_value = [
        {"speaker": "狗與露", "text": "o see so don notet's come", "timestamp": time.time()},
    ]
    await cog._fetch_dj_interjection_raw(_info())
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None
    ctx = call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")
    assert "o see so" not in ctx


@pytest.mark.parametrize("text", [
    "好啊對",                    # 只靠「中文 < 4 字」擋
    "Okay so the build 我覺得好",  # 中文 4 字但比例 < 0.5，只靠比例擋
    "馬文幫我放周杰倫的夜曲",      # 全中文，只靠「對 Marvin 下指令」擋
])
def test_usable_conv_text_each_rule_alone(text):
    assert usable_conv_text(text) is False
