"""TDD: DJ 串場素材聚焦——主素材 1 個 + 歌曲素材抽 1 個，其餘不再無條件附上。

9/30 使用者定案：歌曲類素材（喜好線索/情感記錄/歌詞呼應/歌曲資料/選這首的理由）
跟頻道近期對話過去不管抽到哪個主 mode 都無條件塞進 ctx，實驗證實會讓口白混線
（抽到氛圍卻還扯對話裡的床墊）。改成歌曲素材最多抽 1 個，頻道近期對話只在
mode=="conversation" 時才給。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dj_narration_orchestrator import pick_song_material
from tests.test_dj_story_context import _info, _make_cog, _only, _ctx_str


# ── pick_song_material（純函式） ────────────────────────────────────────────

def test_pick_song_material_empty_candidates_returns_none():
    assert pick_song_material([]) is None


def test_pick_song_material_all_blank_returns_none():
    assert pick_song_material(["", ""]) is None


def test_pick_song_material_picks_via_rng_and_filters_blanks():
    seen = {}

    class _FakeRng:
        @staticmethod
        def choice(pool):
            seen["pool"] = list(pool)
            return pool[0]

    result = pick_song_material(
        ["", "喜好線索：A", "情感記錄：B", ""], rng=_FakeRng()
    )
    assert result == "喜好線索：A"
    assert seen["pool"] == ["喜好線索：A", "情感記錄：B"]


def test_pick_song_material_excludes_text_matching_exclude():
    seen = {}

    class _FakeRng:
        @staticmethod
        def choice(pool):
            seen["pool"] = list(pool)
            return pool[0]

    candidates = ["喜好線索：小明說他超愛陳綺貞", "情感記錄：放鬆"]
    result = pick_song_material(
        candidates, exclude_text="小明說他超愛陳綺貞", rng=_FakeRng()
    )
    assert result == "情感記錄：放鬆"
    assert seen["pool"] == ["情感記錄：放鬆"]


# ── _fetch_dj_interjection_raw：歌曲素材只留 1 個 ───────────────────────────

def _cog_with_song_materials(tmp_path):
    """讓 play_count>=2 / feelings / lyric_match 三個歌曲候選同時存在。"""
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    cog._life_cores = MagicMock(return_value=[])
    cog.bot.music_memory._data = {
        "songs": {
            "song_key_xyz": {
                "requesters": {"大肚": 5},
                "reactions": {
                    "大肚": {
                        "feelings": ["放鬆", "懷念"],
                        "lyric_match": "副歌那句真的唱到心坎",
                    }
                },
            }
        }
    }
    return cog


@pytest.mark.asyncio
async def test_song_materials_capped_to_one(tmp_path, monkeypatch):
    # 9/30 老朋友三槽改版：歌詞（lyric_match）獨立成自己的一槽，不再跟品味類
    # 候選（喜好線索/情感記錄）搶同一個「歌曲素材只抽 1 個」名額，因此改成
    # 分開斷言——品味槽仍恰好 1 個，歌詞槽是唯一候選必中。
    _only(monkeypatch, "atmosphere")
    cog = _cog_with_song_materials(tmp_path)
    await cog._fetch_dj_interjection_raw(_info(requester="大肚"))
    ctx = _ctx_str(cog)
    taste_hit = sum(s in ctx for s in ("喜好線索：這首是", "情感記錄："))
    assert taste_hit == 1, f"品味素材應恰好 1 個: {ctx!r}"
    assert "歌詞呼應：" in ctx, f"歌詞素材（唯一候選）應該入選: {ctx!r}"


@pytest.mark.asyncio
async def test_conversation_lines_absent_when_mode_not_conversation(tmp_path, monkeypatch):
    _only(monkeypatch, "atmosphere")
    cog = _cog_with_song_materials(tmp_path)
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(
        return_value=[{"speaker": "狗與露", "text": "今天天氣真好"}]
    )
    await cog._fetch_dj_interjection_raw(_info(requester="大肚"))
    ctx = _ctx_str(cog)
    assert "頻道近期對話" not in ctx


@pytest.mark.asyncio
async def test_conversation_lines_present_when_mode_is_conversation(tmp_path, monkeypatch):
    _only(monkeypatch, "conversation")
    cog = _cog_with_song_materials(tmp_path)
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(
        return_value=[{"speaker": "狗與露", "text": "今天天氣真好"}]
    )
    await cog._fetch_dj_interjection_raw(_info(requester="大肚"))
    ctx = _ctx_str(cog)
    assert "頻道近期對話" in ctx


@pytest.mark.asyncio
async def test_autopilot_reason_becomes_main_material_when_mode_overridden(tmp_path, monkeypatch):
    """quick/atmosphere 落選 + autopilot 有理由 → select_narration_mode 蓋成 "reason"，
    這時「選這首的理由」是主素材，該進 ctx。"""
    _only(monkeypatch, "quick")
    cog = _cog_with_song_materials(tmp_path)
    info = _info(title="周杰倫 - 夜曲", requester="Marvin推薦（為大肚）")
    info["_explanation"] = "因為大肚最近常點這首"
    await cog._fetch_dj_interjection_raw(info)
    ctx = _ctx_str(cog)
    assert "推薦理由：大家會喜歡這首《夜曲》，理由是因為大肚最近常點這首" in ctx
