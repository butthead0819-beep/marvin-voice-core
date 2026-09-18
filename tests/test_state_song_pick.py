"""TDD: 個人狀態驅動選歌（state pick）→ DJ 口白講出理由。

自動點歌輪到某人時，若他 48h 內有狀態（callback_queue 可分享項 / 非 annoyed 的情緒高光），
LLM 從他自己的候選池挑一首＋一句關心口吻理由，排本輪第一首，DJ 口白用理由開場。
隱私邊界（使用者拍板）：當事人在場才用；taboos、annoyed 一律排除。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dj_topic_selector import TopicCooldownStore
from music_recommender import Candidate
from state_song_pick import (
    build_state_pick_prompt,
    collect_fresh_states,
    parse_state_pick,
)

NOW = 2_000_000_000.0
H = 3600.0


# ── C1. collect_fresh_states ────────────────────────────────────────────────

def _mem(cbq=(), eh=(), taboos=()):
    return {"callback_queue": list(cbq), "emotional_highlights": list(eh), "taboos": list(taboos)}


def test_collect_uses_shareable_callbacks_and_non_annoyed_highlights():
    mem = _mem(
        cbq=[{"text": "等雙十一買打蠟機", "shareable": True, "ts": NOW - 2 * H}],
        eh=[{"moment": "感冒喉嚨痛", "valence": "unwell", "timestamp": NOW - 1 * H}],
    )
    assert collect_fresh_states(mem, now=NOW) == ["感冒喉嚨痛", "等雙十一買打蠟機"]


def test_collect_drops_stale_private_annoyed_meta_and_taboo():
    mem = _mem(
        cbq=[
            {"text": "過期的事", "shareable": True, "ts": NOW - 49 * H},
            {"text": "私密的事", "shareable": False, "ts": NOW - H},
            {"text": "家裡吵架", "shareable": True, "ts": NOW - H},
        ],
        eh=[
            {"moment": "被馬文氣到", "valence": "annoyed", "timestamp": NOW - H},
            {"moment": "__META__ {}", "valence": "warm", "timestamp": NOW - H},
        ],
        taboos=["家裡"],
    )
    assert collect_fresh_states(mem, now=NOW) == []


def test_collect_dedups_keeps_newest_and_caps_at_three():
    mem = _mem(
        cbq=[{"text": f"事{i}", "shareable": True, "ts": NOW - i * H} for i in range(1, 6)]
        + [{"text": "事1", "shareable": True, "ts": NOW - 10 * H}],
    )
    assert collect_fresh_states(mem, now=NOW) == ["事1", "事2", "事3"]


def test_collect_tolerates_missing_and_malformed_fields():
    assert collect_fresh_states({}, now=NOW) == []
    mem = {"callback_queue": "壞", "emotional_highlights": [None, "x", {"moment": "ok"}],
           "taboos": None}
    assert collect_fresh_states(mem, now=NOW) == []


# ── C1. prompt / parse ──────────────────────────────────────────────────────

def test_prompt_lists_person_states_and_numbered_titles():
    sys_p, user_p = build_state_pick_prompt("weakgogo", ["感冒喉嚨痛"], ["歌A", "歌B"])
    assert "weakgogo" in user_p
    assert "0. 感冒喉嚨痛" in user_p
    assert "0. 歌A" in user_p and "1. 歌B" in user_p
    assert "null" in sys_p
    assert "不准" in sys_p


def test_parse_ok_with_surrounding_noise():
    resp = '好的：{"index": 1, "state": 0, "reason": "weakgogo，喉嚨痛就聽慢一點"} 以上'
    assert parse_state_pick(resp, n_titles=2, n_states=1) == (1, 0, "weakgogo，喉嚨痛就聽慢一點")


@pytest.mark.parametrize("payload", [
    {"index": None, "state": 0, "reason": "一句夠長的理由喔"},
    {"index": 5, "state": 0, "reason": "一句夠長的理由喔"},
    {"index": True, "state": 0, "reason": "一句夠長的理由喔"},
    {"index": "1", "state": 0, "reason": "一句夠長的理由喔"},
    {"index": 1, "state": 3, "reason": "一句夠長的理由喔"},
    {"index": 1, "state": 0, "reason": "太短"},
    {"index": 1, "state": 0, "reason": "長" * 61},
])
def test_parse_rejects_invalid(payload):
    assert parse_state_pick(json.dumps(payload, ensure_ascii=False), n_titles=2, n_states=1) is None


def test_parse_rejects_non_json():
    assert parse_state_pick("沒有 JSON", n_titles=2, n_states=1) is None
    assert parse_state_pick("", n_titles=2, n_states=1) is None


# ── C3. _maybe_state_pick ───────────────────────────────────────────────────

class _FakeSuki:
    def __init__(self, players):
        self._players = players

    def has_player(self, name):
        return name in self._players

    def get_player_memory(self, name):
        return self._players[name]


def _make_cog(tmp_path, llm_resp='{"index": 2, "state": 0, "reason": "weakgogo，喉嚨還腫著就聽慢一點的"}'):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.router = MagicMock()
    bot.router._call_llm = AsyncMock(return_value=llm_resp)
    bot.router.memory = _FakeSuki({"weakgogo": _mem(
        eh=[{"moment": "感冒喉嚨痛", "valence": "unwell", "timestamp": __import__("time").time() - H}])})
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    return cog


def _cands(n=4):
    return [Candidate(anchor_title=f"歌{i}", anchor_artist="A", lane="spotlight", mode="direct",
                      target_member="weakgogo", score=1.0) for i in range(n)]


@pytest.mark.asyncio
async def test_state_pick_moves_chosen_first_with_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("MARVIN_STATE_PICK", "1")
    cog = _make_cog(tmp_path)
    cands = _cands()
    out = await cog._maybe_state_pick("weakgogo", ["weakgogo", "大肚"], cands)
    assert [c.anchor_title for c in out] == ["歌2", "歌0", "歌1", "歌3"]
    assert out[0].state_reason == "weakgogo，喉嚨還腫著就聽慢一點的"
    assert all(c.state_reason == "" for c in out[1:])
    assert "speaker" not in cog.bot.router._call_llm.call_args.kwargs


@pytest.mark.asyncio
async def test_state_pick_off_when_env_not_set(tmp_path, monkeypatch):
    monkeypatch.delenv("MARVIN_STATE_PICK", raising=False)
    cog = _make_cog(tmp_path)
    cands = _cands()
    assert await cog._maybe_state_pick("weakgogo", ["weakgogo"], cands) == cands
    cog.bot.router._call_llm.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("spotlight,members", [
    ("weakgogo", ["大肚"]),                          # 當事人不在場
    ("Marvin推薦（為weakgogo）", ["Marvin推薦（為weakgogo）"]),  # 偽玩家
    ("陌生人", ["陌生人"]),                          # 沒有記憶紀錄
])
async def test_state_pick_skips_when_not_eligible(tmp_path, monkeypatch, spotlight, members):
    monkeypatch.setenv("MARVIN_STATE_PICK", "1")
    cog = _make_cog(tmp_path)
    cands = _cands()
    assert await cog._maybe_state_pick(spotlight, members, cands) == cands
    cog.bot.router._call_llm.assert_not_called()


@pytest.mark.asyncio
async def test_state_pick_person_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("MARVIN_STATE_PICK", "1")
    cog = _make_cog(tmp_path, llm_resp='{"index": null, "state": 0, "reason": "沒有合適的歌"}')
    cands = _cands()
    assert await cog._maybe_state_pick("weakgogo", ["weakgogo"], cands) == cands  # LLM 回 null
    assert cog.bot.router._call_llm.call_count == 1
    assert await cog._maybe_state_pick("weakgogo", ["weakgogo"], cands) == cands  # 冷卻中
    assert cog.bot.router._call_llm.call_count == 1


@pytest.mark.asyncio
async def test_state_pick_llm_exception_returns_original(tmp_path, monkeypatch):
    monkeypatch.setenv("MARVIN_STATE_PICK", "1")
    cog = _make_cog(tmp_path)
    cog.bot.router._call_llm = AsyncMock(side_effect=RuntimeError("boom"))
    cands = _cands()
    assert await cog._maybe_state_pick("weakgogo", ["weakgogo"], cands) == cands


# ── C5. DJ 口白接線 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dj_context_uses_state_reason_as_memory_evidence(tmp_path):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="weakgogo，喉嚨還腫著，這首慢的給你")
    bot.router.memory = _FakeSuki({})
    bot.engine = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="k")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    info = {"title": "A - 歌", "uploader": "A", "url": "https://example/x",
            "requested_by": "Marvin推薦（為weakgogo）", "_spotlight": "weakgogo",
            "_state_reason": "weakgogo，喉嚨還腫著就聽慢一點的"}
    with patch.object(cog, "_dj_clean_name", return_value=("歌", "A")):
        await cog._fetch_dj_interjection_raw(info)
    ctx = bot.router.generate_dynamic_system_msg.call_args.kwargs.get("context", "")
    assert "記憶證據" in ctx
    assert "weakgogo，喉嚨還腫著就聽慢一點的" in ctx


# ── C4. _auto_recommend 接線守門（該方法依賴太多難以整條跑；守住兩個接點沒被刪）──

def test_auto_recommend_wires_state_pick_on_tier1_and_carries_reason():
    import inspect
    from cogs.music_cog_story_arc import MusicStoryArcMixin
    src = inspect.getsource(MusicStoryArcMixin._auto_recommend)
    tier1 = src.split("if _tier == 1:\n            cands = pick_candidates", 1)[1].split("elif _tier == 2:", 1)[0]
    assert "self._maybe_state_pick(spotlight, members, cands)" in tier1
    assert "info['_state_reason'] = cand.state_reason" in src
