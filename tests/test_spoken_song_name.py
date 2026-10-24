"""TTS 念歌名的最後一道閘（song_name_clean.speakable_title/speakable_artist/spoken_name/
spoken_song_name）：清洗後仍像 YouTube 標題就不念，寧可不提歌名也不唸髒的。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from song_name_clean import speakable_artist, speakable_title, spoken_name, spoken_song_name


# ── speakable_title：念不出口 ────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "偏偏喜歡你 | 夜色钢琴曲 Night Piano",
    "梦飞船『超高无损音質』 Pinyin中文English英Lyrics",
    "左邊 原唱：楊丞琳",
    "蔡琴 小提琴 Violin Cover by Momo",
    "《兄弟的悲歌》#角頭電影",
    "五月天 Mayday happy.BIRTH.day 希望工場 Home Maker 主題曲",
    "偏偏要 怪我偏偏要愛 偏偏要等待 偏偏要看你不會來的海",
    "📽長路漫漫伴你闖",
    "Matzka《⽔災 The Flood》Official",
    "",
])
def test_speakable_title_rejects(title):
    assert speakable_title(title) is None


# ── speakable_title：念得出口 ────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "甲乙丙丁",
    "囍帖街 (Live)",
    "斷點(煙嗓版)",
    "BIRDS OF A FEATHER",
    "一直很安靜·2023",
    "雨衣",
    "Can't Help Falling In Love",
    "在水一方 The Unforgettable Character",
    "共身軀完全放予去 Let the Body Speaks",
])
def test_speakable_title_accepts_unchanged(title):
    assert speakable_title(title) == title


# ── speakable_title：剝尾端註記 ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("一念(電視劇《逐玉》插曲)", "一念"),
    ("蝴蝶 (《基因決定我愛你》片尾曲)", "蝴蝶"),
    ('如果可以 (電影"月老"主題曲)', "如果可以"),
    ("Everything BoZ (feat. Coi Leray)", "Everything BoZ"),
    ("借影 (《嫁金釵》電視劇慟情曲)", "借影"),
])
def test_speakable_title_strips_tail_note(raw, expected):
    assert speakable_title(raw) == expected


# ── speakable_artist ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("artist", [
    "Release - Topic",
    "謝宜君 豪記官方專屬頻道",
    "Sony Music China索尼音樂中國",
    "Haohao Music",
    "GM Lyric",
    "张碧晨资讯站-Diamond Station",
])
def test_speakable_artist_rejects(artist):
    assert speakable_artist(artist) == ""


@pytest.mark.parametrize("artist", ["阿杜", "李佳薇", "Billie Eilish", "理想混蛋 Bestards"])
def test_speakable_artist_accepts_unchanged(artist):
    assert speakable_artist(artist) == artist


# ── spoken_name / spoken_song_name ──────────────────────────────────────────

@pytest.mark.parametrize("title", ["Topic - 时间胶囊"])   # 「Release - Topic - 时间胶囊」拆錯的歌名
def test_speakable_title_rejects_topic_fragment(title):
    assert speakable_title(title) is None


def test_speakable_artist_rejects_generic_release():
    assert speakable_artist("Release") == ""


def test_spoken_name_dirty_title_returns_none_tuple():
    assert spoken_name("偏偏喜歡你 | 夜色钢琴曲 Night Piano", "周興哲") == (None, "")


def test_spoken_name_clean_title_keeps_artist_gated_separately():
    assert spoken_name("雨衣", "Release - Topic") == ("雨衣", "")
    assert spoken_name("雨衣", "阿杜") == ("雨衣", "阿杜")


def _vid(url):
    return url or None


def test_spoken_song_name_uses_canon_when_present():
    info = {"_canon": {"title": "雨衣", "artist": "阿杜"}, "title": "不管的垂死標題 Official MV"}
    assert spoken_song_name(info, extract_vid=_vid, catalog_index={}) == ("雨衣", "阿杜")


def test_spoken_song_name_falls_back_to_dj_display_name_without_canon():
    info = {"title": "雨衣", "uploader": "阿杜"}
    assert spoken_song_name(info, extract_vid=_vid, catalog_index={}) == ("雨衣", "阿杜")


def test_spoken_song_name_dirty_returns_none():
    info = {"title": "偏偏喜歡你 | 夜色钢琴曲 Night Piano", "uploader": "周興哲"}
    assert spoken_song_name(info, extract_vid=_vid, catalog_index={}) == (None, "")


# ── 呼叫端：_dj_ctx_song_line（staticmethod，不需要 cog 實例） ─────────────────

def test_dj_ctx_song_line_clean():
    from cogs.music_cog import MusicCog
    assert MusicCog._dj_ctx_song_line("雨衣", "阿杜") == "歌曲：阿杜 - 雨衣"
    assert MusicCog._dj_ctx_song_line("雨衣", "") == "歌曲：雨衣"


def test_dj_ctx_song_line_dirty_forbids_song_name():
    from cogs.music_cog import MusicCog
    line = MusicCog._dj_ctx_song_line("偏偏喜歡你 | 夜色钢琴曲 Night Piano", "周興哲")
    assert "偏偏喜歡你" not in line
    assert "不准提歌名" in line


# ── 呼叫端：_quick_segue_template no_song 篩選 ────────────────────────────────

def test_quick_segue_template_no_song_excludes_song_placeholder():
    from cogs.music_cog import MusicCog
    for n in (1, 2, 5):
        for _ in range(20):
            assert "{song}" not in MusicCog._quick_segue_template(n, no_song=True)


def test_quick_segue_template_no_song_overrides_need_song():
    from cogs.music_cog import MusicCog
    for _ in range(20):
        assert "{song}" not in MusicCog._quick_segue_template(1, need_song=True, no_song=True)


# ── 呼叫端：fixed_announcement / quick（透過 _fetch_dj_interjection_raw） ──────

from tests.test_dj_story_context import _make_cog as _make_dj_cog, _no_quick, _only

DIRTY_TITLE = "偏偏喜歡你 | 夜色钢琴曲 Night Piano"


def _dirty_info(**kw):
    d = {"title": DIRTY_TITLE, "requested_by": "狗與露", "uploader": "周興哲-Topic",
         "webpage_url": "https://www.youtube.com/watch?v=zzzzzzzzzzz"}
    d.update(kw)
    return d


@pytest.mark.asyncio
async def test_fixed_announcement_no_song_when_dirty(tmp_path, monkeypatch):
    """LLM 空手且歌名念不出口 → 保底報幕只說「下一首歌」，不念髒標題。"""
    _no_quick(monkeypatch)
    cog = _make_dj_cog(tmp_path=tmp_path)
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="")
    out = await cog._fetch_dj_interjection_raw(_dirty_info())
    assert "《" not in out["text"]
    assert "下一首歌" in out["text"]
    llm_ctx = cog.bot.router.generate_dynamic_system_msg.await_args.kwargs["context"]
    assert DIRTY_TITLE not in llm_ctx and "Night Piano" not in llm_ctx
    assert "不准提歌名" in llm_ctx


@pytest.mark.asyncio
async def test_quick_mode_no_song_when_dirty(tmp_path, monkeypatch):
    """quick 模式（跳過 LLM）歌名念不出口 → 選不含 {song} 的模板，不念髒標題。"""
    _only(monkeypatch, "quick")
    import random
    monkeypatch.setattr(random, "choice", lambda seq: next((t for t in seq if "{song}" in t), seq[0]))
    cog = _make_dj_cog(tmp_path=tmp_path)
    out = await cog._fetch_dj_interjection_raw(_dirty_info())
    assert "《" not in out["text"]
    assert "{song}" not in out["text"]
    assert DIRTY_TITLE not in out["text"]


@pytest.mark.asyncio
async def test_quick_mode_still_speaks_clean_song(tmp_path, monkeypatch):
    """乾淨歌名仍正常帶歌名播出（確保閘沒有誤殺）。"""
    _only(monkeypatch, "quick")
    import random
    monkeypatch.setattr(random, "choice", lambda seq: next((t for t in seq if "{song}" in t), seq[0]))
    cog = _make_dj_cog(tmp_path=tmp_path)
    info = _dirty_info(title="雨衣", uploader="阿杜")
    out = await cog._fetch_dj_interjection_raw(info)
    assert "雨衣" in out["text"]


async def _slow_meta(info):
    import asyncio
    await asyncio.sleep(10)


# ── 呼叫端：tail timeout fallback（_meta_with_ack_fallback） ──────────────────

@pytest.mark.asyncio
async def test_tail_timeout_fallback_no_song_when_dirty(tmp_path):
    cog = _make_dj_cog(tmp_path=tmp_path)
    cog._fetch_song_meta = _slow_meta
    cog._COLD_META_TIMEOUT_S = 0.01
    out = await cog._meta_with_ack_fallback(_dirty_info(), "狗與露")
    assert "《" not in out["dj"]["text"]
    assert "下一首，" in out["dj"]["text"]


@pytest.mark.asyncio
async def test_tail_timeout_fallback_keeps_clean_song(tmp_path):
    cog = _make_dj_cog(tmp_path=tmp_path)
    cog._fetch_song_meta = _slow_meta
    cog._COLD_META_TIMEOUT_S = 0.01
    info = _dirty_info(title="雨衣", uploader="阿杜")
    out = await cog._meta_with_ack_fallback(info, "狗與露")
    assert "《雨衣》" in out["dj"]["text"]


@pytest.mark.asyncio
async def test_tail_consistency_guard_safe_text_no_dirty_song(tmp_path):
    import asyncio
    cog = _make_dj_cog(tmp_path=tmp_path)
    info = _dirty_info(url="u-next")
    fut = asyncio.get_running_loop().create_future()
    fut.set_result({"dj": {"prev_title_used": "上一首A", "audio_path": "x"}})
    cog._prefetch_cache = {"u-next": fut}
    audio = tmp_path / "safe.mp3"
    audio.write_bytes(b"x")
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value=str(audio))
    meta = await cog._resolve_tail_dj_meta(info, cur_info={"title": "實際是B"})
    assert meta is not None
    assert "Night Piano" not in meta["text"] and "Topic" not in meta["text"]
    assert "下一首歌" in meta["text"]


# ── 呼叫端：_speak_song_ack ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_song_ack_no_song_when_dirty(tmp_path):
    cog = _make_dj_cog(tmp_path=tmp_path)
    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock()
    await cog._speak_song_ack(vc, _dirty_info())
    ack_text = cog.bot.tts_engine.generate_audio.await_args.args[0]
    assert ack_text == "幫你點好了"


@pytest.mark.asyncio
async def test_speak_song_ack_keeps_clean_song(tmp_path):
    cog = _make_dj_cog(tmp_path=tmp_path)
    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock()
    info = _dirty_info(title="雨衣", uploader="阿杜")
    await cog._speak_song_ack(vc, info)
    ack_text = cog.bot.tts_engine.generate_audio.await_args.args[0]
    assert ack_text == "幫你點了《雨衣》"


# ── 呼叫端：build_relation / render_relation ─────────────────────────────────

from dj_relation import Relation, build_relation, render_relation
from music_memory import extract_video_id
from song_name_clean import dj_display_name


def _display(entry):
    return dj_display_name(entry, extract_vid=extract_video_id)


def _song(title, requesters, *, url="https://www.youtube.com/watch?v=aaaaaaaaaaa"):
    return {"title": title, "uploader": "x", "webpage_url": url, "requesters": requesters}


def test_build_relation_none_when_main_title_dirty():
    songs = {"u1": _song("周傳雄 - 黃昏", {"showay": 3})}
    rel = build_relation(songs=songs, title=DIRTY_TITLE, artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel is None


def test_build_relation_skips_anchor_when_anchor_dirty():
    dirty_anchor = "某歌手 - 梦飞船『超高无损音質』 Pinyin中文English英Lyrics"
    songs = {
        "u1": _song(dirty_anchor, {"showay": 3}, url="https://www.youtube.com/watch?v=bbbbbbbbbbb"),
        "u2": _song("周傳雄 - 黃昏", {}, url="https://www.youtube.com/watch?v=aaaaaaaaaaa"),
    }
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display,
                          anchor_title=dirty_anchor)
    assert rel is None or rel.kind != "anchor_song"


def test_build_relation_skips_same_artist_when_artist_dirty():
    songs = {
        "u1": _song("Release - 歌一", {"showay": 1}, url="https://www.youtube.com/watch?v=bbbbbbbbbbb"),
        "u2": _song("Release - 歌二", {"showay": 1}, url="https://www.youtube.com/watch?v=ccccccccccc"),
        "u3": _song("Release - 歌三", {"showay": 1}, url="https://www.youtube.com/watch?v=ddddddddddd"),
    }
    rel = build_relation(songs=songs, title="黃昏", artist="Release",
                          present={"showay", "狗與露"}, display=_display)
    assert rel is None


def test_render_relation_gates_artist_in_song_phrase():
    from cogs.music_cog import MusicCog
    lines = MusicCog._RELATION_LINES
    rel = Relation("listens_song", "showay", "雨衣", "Release - Topic")
    for _ in range(20):
        text, tmpl = render_relation(rel, lines)
        assert "Release" not in text
        assert "《雨衣》" in text



def test_render_relation_speaks_stripped_title_and_anchor():
    """閘放行的是剝掉尾端註記的版本，念出來也要是那個版本，不能念回原始 title。"""
    from cogs.music_cog import MusicCog
    lines = MusicCog._RELATION_LINES
    rel = Relation("listens_song", "showay", "一念(電視劇《逐玉》插曲)", "張紫寧")
    for _ in range(20):
        text, _t = render_relation(rel, lines)
        assert "逐玉" not in text and "《一念》" in text
    rel = Relation("anchor_song", "showay", "雨衣", "阿杜", anchor="蝴蝶 (《基因決定我愛你》片尾曲)")
    for _ in range(20):
        text, _t = render_relation(rel, lines)
        assert "基因" not in text

# ── 呼叫端：explanation_slotfill._compute_slot_values seed_title ─────────────

def _evidence(**kw):
    from music_recommender import Evidence
    base = dict(signal_type="radio_related", timestamp=None, play_count=0, skip_count=0,
                source_tier="adjacent_artist", subject="you_all", requester=None, seed_title=None)
    base.update(kw)
    return Evidence(**base)


def test_compute_slot_values_rejects_dirty_seed_title():
    from explanation_slotfill import _compute_slot_values
    values = _compute_slot_values(_evidence(seed_title=DIRTY_TITLE))
    assert "seed_title" not in values


def test_compute_slot_values_keeps_clean_seed_title():
    from explanation_slotfill import _compute_slot_values
    values = _compute_slot_values(_evidence(seed_title="雨衣（Official MV）"))
    assert values["seed_title"] == "雨衣"


# ── 呼叫端：_handle_music_info_query（文字頻道保留原標題，TTS 念乾淨版） ───────

from tests.test_followup_listening import _make_cog as _make_vc_cog


@pytest.mark.asyncio
async def test_music_info_query_tts_speaks_clean_text_only():
    cog = _make_vc_cog()
    cog._current_stream_info = {
        "title": DIRTY_TITLE, "uploader": "周興哲-Topic", "requested_by": "狗與露",
        "webpage_url": "https://www.youtube.com/watch?v=zzzzzzzzzzz",
    }
    cog.play_tts = AsyncMock()
    await cog._handle_music_info_query("狗與露", "這首歌叫什麼")
    spoken_text = cog.play_tts.await_args.args[0]
    assert "|" not in spoken_text
    assert "Night Piano" not in spoken_text
    channel_msg = cog.active_text_channel.send.await_args.args[0]
    assert DIRTY_TITLE in channel_msg


@pytest.mark.asyncio
async def test_music_info_query_tts_speaks_clean_song_with_title():
    cog = _make_vc_cog()
    cog._current_stream_info = {
        "title": "雨衣", "uploader": "阿杜", "requested_by": "狗與露",
        "webpage_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
    }
    cog.play_tts = AsyncMock()
    await cog._handle_music_info_query("狗與露", "這首歌叫什麼")
    spoken_text = cog.play_tts.await_args.args[0]
    assert "雨衣" in spoken_text
    assert "阿杜" in spoken_text
