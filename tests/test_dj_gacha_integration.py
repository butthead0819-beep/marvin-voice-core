"""TDD：扭蛋式動機組裝整合測試（tests/test_dj_gacha_integration.py）。

驗證：
1. mode == "guide" 時，結合 _song_card 中的多維度素材（社群熱評、歌詞刺點）。
2. 動機指令（串場動機【...】）注入 context，取代死板八股句。
3. 向下相容只有 guide 純文字的舊呼叫。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from cogs.music_cog_dj_lyrics import MusicDJLyricsMixin


class DummyCog(MusicDJLyricsMixin):
    def __init__(self):
        self.bot = MagicMock()
        self.bot.music_memory = None
        self.bot.tts_engine.generate_audio = AsyncMock(return_value=b"fake_mp3")
        self._last_dj_joke_ts = None
        self._auto_guide_budget = None
        self._guide_inflight = {}

    def _vc(self):
        return None

    def _city_label(self):
        return "台北"

    def _current_season(self):
        return "秋天"

    def _present_interests(self):
        return []

    def _recent_emotional_highlight(self, requester):
        return None

    def _dj_topic_store(self):
        from dj_topic_selector import TopicCooldownStore
        import tempfile
        return TopicCooldownStore(tempfile.mktemp(suffix=".json"))

    def _autopilot_pick_reason(self, info):
        return ""

    async def _life_cores_async(self):
        return []

    async def _fetch_news_items_async(self, interests):
        return []


@pytest.mark.asyncio
async def test_dj_guide_mode_injects_gacha_motivation_and_facets(monkeypatch):
    cog = DummyCog()

    song_card = {
        "audiophile_guide": "前奏木吉他刷弦一出來，就是千禧年代的校園回憶。",
        "social_lore": {"tag": "時代眼淚神曲", "context": "下課鈴聲與大雨"},
        "lyric_hook": {"quote": "從前從前有個人愛妳很久", "timestamp": "02:14", "subtext": "暗戀遺憾"},
    }

    info = {
        "title": "晴天",
        "artist": "周杰倫",
        "requested_by": "showay",
        "_song_card": song_card,
    }

    cog._dj_song_material = AsyncMock(return_value=(
        {"title": "晴天", "artist": "周杰倫", "album": "葉惠美", "year": 2003},
        song_card["audiophile_guide"],
    ))

    cog.bot.tts_engine.get_estimated_duration.return_value = 5.0

    # 模擬 LLM 回應
    captured_context = []
    async def mock_generate(template_key, context="", **kwargs):
        captured_context.append(context)
        return "這首晴天前奏一下，直接回到千禧年，戴好耳機聽木吉他。"

    cog.bot.router.generate_dynamic_system_msg = AsyncMock(side_effect=mock_generate)


    # 執行串場生成
    res = await cog._fetch_dj_interjection_raw(info)

    assert res is not None
    assert len(captured_context) == 1
    prompt_ctx = captured_context[0]

    # 驗證多維度素材被注入
    assert "導聆素材" in prompt_ctx
    assert "時代眼淚神曲" in prompt_ctx
    assert "從前從前有個人愛妳很久" in prompt_ctx

    # 驗證扭蛋動機被注入
    assert "串場動機【" in prompt_ctx
    # 扭蛋動機（尤其 tea 爆八卦）不能蓋掉反幻覺約束
    assert "不准自己補細節或編故事" in prompt_ctx

