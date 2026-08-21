"""TDD: 歌曲深度知識抽取、維基百科補全與知識庫快取測試

測試範圍：
1. song_metadata_extractor: 本地 YouTube 簡介正則抽取（詞/曲/編/製/專輯/年份/官方文案故事）
2. song_metadata_extractor: 噪訊過濾（社群連結、MV 工作人員、訂閱提示過濾）
3. wikipedia_music_fetcher: 維基百科 API 資料解析與摘要清洗
4. song_knowledge_store: 本地 JSON 讀寫、快取管理與 insight 格式化輸出
"""
from __future__ import annotations

import tempfile
import pytest
from unittest.mock import patch, AsyncMock

from song_metadata_extractor import extract_song_metadata_from_description, clean_story_snippet
from wikipedia_music_fetcher import parse_wikipedia_summary, fetch_wikipedia_music_summary
from song_knowledge_store import SongKnowledgeStore, format_music_insight


# ── 1. YouTube Metadata Extractor 測試 ──────────────────────────────

def test_extract_credits_chinese_standard():
    """標準中文格式的作詞、作曲、編曲、製作人抽取。"""
    desc = """
    周杰倫【夜曲】官方完整版 MV
    
    作詞：方文山
    作曲：周杰倫
    編曲：林邁可
    製作人：周杰倫
    收錄於專輯《十一月的蕭邦》
    
    以蕭邦著名的夜曲為靈感創作，融合了古典鋼琴與哥德式抒情嘻哈風格。
    
    訂閱杰威爾音樂: http://bit.ly/JVRMusic
    """
    res = extract_song_metadata_from_description(desc)
    assert res.get("lyricist") == "方文山"
    assert res.get("composer") == "周杰倫"
    assert res.get("arranger") == "林邁可"
    assert res.get("producer") == "周杰倫"
    assert res.get("album") == "十一月的蕭邦"
    assert "以蕭邦著名的夜曲為靈感創作" in (res.get("story_snippet") or "")
    assert "訂閱" not in (res.get("story_snippet") or "")
    assert "http" not in (res.get("story_snippet") or "")


def test_extract_credits_english_and_slash():
    """英文與斜線格式（Lyrics / Music / Producer）抽取。"""
    desc = """
    Official Music Video for "Fix You" by Coldplay
    
    Lyrics by Chris Martin
    Music: Guy Berryman / Jonny Buckland / Will Champion / Chris Martin
    Producer: Ken Nelson
    From the album "X&Y" (2005)
    
    Written by Chris Martin for his then-wife Gwyneth Paltrow after the death of her father.
    
    Follow Coldplay:
    https://coldplay.com
    """
    res = extract_song_metadata_from_description(desc)
    assert res.get("lyricist") == "Chris Martin"
    assert "Chris Martin" in (res.get("composer") or "")
    assert res.get("producer") == "Ken Nelson"
    assert res.get("album") == "X&Y"
    assert "Written by Chris Martin" in (res.get("story_snippet") or "")
    assert "https://" not in (res.get("story_snippet") or "")


def test_clean_story_snippet_filters_crew_and_boilerplate():
    """文案過濾應排除導演、化妝、攝影、版權與社群宣傳。"""
    dirty_text = """
    導演：陳映之
    攝影：林志堅
    化妝：張小美
    
    這首歌曲記錄了在城市漂泊青年對於未來的迷茫與希望，充滿力量。
    
    關注官方 IG: @artist_official
    數位平台全面上線：https://linktr.ee/music
    版權所有 翻印必究
    """
    snippet = clean_story_snippet(dirty_text)
    assert "城市漂泊青年" in snippet
    assert "導演" not in snippet
    assert "IG" not in snippet
    assert "linktr.ee" not in snippet


# ── 2. Wikipedia Music Fetcher 測試 ────────────────────────────────

def test_parse_wikipedia_summary():
    """解析維基百科 API 回傳的 extract 文本。"""
    api_extract = """
    《晴天》（英語：Sunny Day）是台灣男歌手周杰倫的一首歌曲，收錄於他的第四張錄音室專輯《葉惠美》中。該歌曲由周杰倫親自作詞、作曲及編曲。該曲榮獲第十五屆金曲獎最佳作詞人獎提名。這首歌以校園青澀的戀愛與遺憾為主題。
    """
    story = parse_wikipedia_summary(api_extract)
    assert story is not None
    assert "校園青澀的戀愛" in story or "葉惠美" in story
    assert "英語：Sunny Day" not in story  # 應清理括號內的英語註解噪訊


@pytest.mark.asyncio
async def test_fetch_wikipedia_music_summary_mocked():
    """模擬 Wikipedia API 查詢與提取。"""
    fake_json = {
        "query": {
            "pages": {
                "12345": {
                    "title": "晴天 (歌曲)",
                    "extract": "《晴天》是周杰倫演唱的經典校園抒情歌曲，描寫青春的遺憾與回憶。"
                }
            }
        }
    }
    with patch("wikipedia_music_fetcher._query_wikipedia_api", new_callable=AsyncMock) as mock_api:
        mock_api.return_value = fake_json
        summary = await fetch_wikipedia_music_summary("晴天", "周杰倫")
        assert summary is not None
        assert "校園抒情歌曲" in summary


# ── 3. Song Knowledge Store 測試 ────────────────────────────────────

def test_format_music_insight_all_fields():
    """格式化輸出音樂賞析字串。"""
    data = {
        "composer": "周杰倫",
        "lyricist": "方文山",
        "arranger": "林邁可",
        "album": "十一月的蕭邦",
        "story_snippet": "以蕭邦夜曲為靈感，融合古典鋼琴與哥德式嘻哈",
    }
    insight = format_music_insight(data)
    assert insight is not None
    assert "方文山作詞" in insight or "方文山" in insight
    assert "周杰倫作曲" in insight or "周杰倫" in insight
    assert "十一月的蕭邦" in insight
    assert "以蕭邦夜曲為靈感" in insight


def test_song_knowledge_store_persistence():
    """測試本地 JSON 快取的讀取與寫入。"""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        store = SongKnowledgeStore(path=tmp.name)
        assert store.get("周杰倫 - 夜曲") is None
        
        entry = {
            "composer": "周杰倫",
            "lyricist": "方文山",
            "album": "十一月的蕭邦",
        }
        store.set("周杰倫 - 夜曲", entry)
        
        # 重新讀取
        store2 = SongKnowledgeStore(path=tmp.name)
        cached = store2.get("周杰倫 - 夜曲")
        assert cached is not None
        assert cached.get("lyricist") == "方文山"
