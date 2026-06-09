"""TDD: autopilot 推薦歌曲的 DJ intro 應包含 spotlight 成員名稱

行為規格：
- info 有 _spotlight + _round_first=True + requester 以 "Marvin" 開頭
  → DJ intro 文字應包含 spotlight 名字
- spotlight 為空 → 用「你」兜底，仍能播出
- 有 clean_artist → 文字包含藝人名稱
- 無 clean_artist → 文字包含歌曲名稱
- group_resonance lane → 用「大家」，不點名個人
"""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch


def _make_cog():
    """最小 VoiceController stub，只為測 _autopilot_dj_phrase。"""
    from cogs.voice_controller import VoiceController
    cog = VoiceController.__new__(VoiceController)
    return cog


class TestAutopilotDjPhrase:
    """_autopilot_dj_phrase 靜態方法行為測試。"""

    def test_spotlight_name_in_phrase(self):
        """spotlight='Alice' → 回傳文字包含 Alice。"""
        cog = _make_cog()
        result = cog._autopilot_dj_phrase("Alice", "天天", "陶喆")
        assert "Alice" in result, f"缺少 spotlight 名稱，got: {result!r}"

    def test_artist_in_phrase(self):
        """有 artist → 文字包含藝人。"""
        cog = _make_cog()
        result = cog._autopilot_dj_phrase("Jack", "天天", "陶喆")
        assert "陶喆" in result, f"缺少藝人名稱，got: {result!r}"
        assert "天天" in result, f"缺少歌名，got: {result!r}"

    def test_no_artist_uses_title(self):
        """無 artist → 至少包含歌名。"""
        cog = _make_cog()
        result = cog._autopilot_dj_phrase("Jack", "天天", "")
        assert "天天" in result, f"無 artist 時缺歌名，got: {result!r}"

    def test_empty_spotlight_falls_back(self):
        """spotlight='' → 回傳非空字串（用「你」兜底）。"""
        cog = _make_cog()
        result = cog._autopilot_dj_phrase("", "天天", "陶喆")
        assert result.strip(), "空 spotlight 應有兜底文字"
        assert "天天" in result or "陶喆" in result

    def test_group_phrase_no_personal_name(self):
        """lane='group_resonance' → 不應出現個人名字，但仍包含歌名。"""
        cog = _make_cog()
        result = cog._autopilot_dj_phrase("Alice", "天天", "陶喆", lane="group_resonance")
        assert "Alice" not in result, f"group_resonance 不應點名，got: {result!r}"
        assert "天天" in result or "陶喆" in result


@pytest.mark.asyncio
async def test_dj_interjection_includes_spotlight(monkeypatch):
    """_fetch_dj_interjection_raw 在 Marvin round_first 路徑，text 應含 spotlight 名。"""
    from cogs.voice_controller import VoiceController
    cog = VoiceController.__new__(VoiceController)

    # 最小 bot stub
    bot = MagicMock()
    bot.music_memory = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = MagicMock(return_value=None)
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    # generate_audio 是 async
    import asyncio
    bot.tts_engine.generate_audio = MagicMock(side_effect=lambda t: asyncio.coroutine(lambda: None)())

    async def _fake_generate(t):
        return None
    bot.tts_engine.generate_audio = _fake_generate

    cog.bot = bot

    # _parse_song_title_artist 回傳固定值
    cog._parse_song_title_artist = MagicMock(return_value=("天天", "陶喆"))

    # TTS gate mock（回傳原文，未截斷）
    with patch("tts_length_policy.truncate_for_tts", return_value=("天天陶喆text", False)):
        info = {
            'requested_by': 'Marvin推薦（為Alice）',
            '_round_first': True,
            '_spotlight': 'Alice',
            'title': '天天',
        }
        result = await cog._fetch_dj_interjection_raw(info)

    assert result is not None
    assert "Alice" in result['text'], f"DJ text 缺少 spotlight，got: {result['text']!r}"
