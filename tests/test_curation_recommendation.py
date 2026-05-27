"""TDD: build_curation_recommendation — 把成功的 CURATION/DIRECTIONAL resolve
包成 Recommendation（vector intent Step 4，offline feedback log）。

純函式測試，不需實例化 VoiceController。
"""
from __future__ import annotations

from intent_agents.semantic_resolver import ResolvedIntent
from intent_bus import IntentContext
from cogs.voice_controller import build_curation_recommendation


def _ctx(query, speaker="大肚"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0,
    )


def test_curation_recommendation_maps_fields():
    ctx = _ctx("播放周杰倫")
    resolved = ResolvedIntent(rewritten_query="播放周杰倫的夜曲", quip="嘆氣 又懷舊",
                              depth=1, selected="夜曲")

    rec = build_curation_recommendation("song_choice", ctx, resolved, now=123.0)

    assert rec.agent == "music"
    assert rec.speaker == "大肚"
    assert rec.trigger == "curation"
    assert rec.selected == "夜曲"
    assert rec.explanation_uttered == "嘆氣 又懷舊"
    assert rec.ts == 123.0
    assert rec.feedback_window_s == 300
    assert rec.channel_state["depth"] == 1
    assert "播放周杰倫" in rec.reason_internal


def test_directional_recommendation_trigger():
    ctx = _ctx("播放周杰倫符合我年紀的歌")
    resolved = ResolvedIntent(rewritten_query="播放周杰倫的七里香", depth=1, selected="七里香")

    rec = build_curation_recommendation("directional_resolution", ctx, resolved, now=1.0)

    assert rec.trigger == "directional"
    assert rec.selected == "七里香"


def test_selected_falls_back_to_rewritten_when_empty():
    """resolver 沒給乾淨曲名 → selected 退回 rewritten_query，不留空。"""
    ctx = _ctx("播放周杰倫")
    resolved = ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1, selected="")

    rec = build_curation_recommendation("song_choice", ctx, resolved, now=1.0)

    assert rec.selected == "播放周杰倫的夜曲"


# ── Phase 1 豐富化：time_of_day 自動加入 + channel_state_extras kwarg ──────


def test_curation_recommendation_adds_time_of_day_automatically():
    """build 函數應該自動把 time_of_day 加入 channel_state，caller 不用手動算。"""
    import datetime
    tpe = datetime.timezone(datetime.timedelta(hours=8))
    # 2026-05-28 08:00 UTC+8 → morning
    ts_morning = datetime.datetime(2026, 5, 28, 8, 0, tzinfo=tpe).timestamp()

    ctx = _ctx("播放周杰倫")
    resolved = ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1, selected="夜曲")
    rec = build_curation_recommendation("song_choice", ctx, resolved, now=ts_morning)

    assert rec.channel_state["time_of_day"] == "morning"


def test_curation_recommendation_accepts_channel_state_extras():
    """caller 可以傳 extras 把 controller scope 的 rich context 塞進來。"""
    ctx = _ctx("播放周杰倫")
    resolved = ResolvedIntent(rewritten_query="播放周杰倫的夜曲", depth=1, selected="夜曲")
    rec = build_curation_recommendation(
        "song_choice", ctx, resolved, now=1.0,
        channel_state_extras={
            "recent_history_titles": ["稻香", "晴天"],
            "queue_depth": 2,
        },
    )

    assert rec.channel_state["depth"] == 1  # 既有欄位保留
    assert rec.channel_state["recent_history_titles"] == ["稻香", "晴天"]
    assert rec.channel_state["queue_depth"] == 2
    # time_of_day 也要存在
    assert "time_of_day" in rec.channel_state


def test_curation_extras_does_not_override_essential_fields():
    """extras 不該被允許覆寫 build 自動填的 essential 欄位（depth / time_of_day）。
    避免 caller 傳錯把推薦邏輯資料污染。"""
    ctx = _ctx("播放周杰倫")
    resolved = ResolvedIntent(rewritten_query="x", depth=5, selected="夜曲")
    rec = build_curation_recommendation(
        "song_choice", ctx, resolved, now=1.0,
        channel_state_extras={"depth": 99, "time_of_day": "garbage"},
    )
    assert rec.channel_state["depth"] == 5  # 從 resolved 來，不被覆寫
    assert rec.channel_state["time_of_day"] != "garbage"


def test_curation_extras_none_works():
    """不傳 extras 也行，向後相容。"""
    ctx = _ctx("播放周杰倫")
    resolved = ResolvedIntent(rewritten_query="x", depth=1, selected="夜曲")
    rec = build_curation_recommendation("song_choice", ctx, resolved, now=1.0,
                                         channel_state_extras=None)
    assert rec.channel_state["depth"] == 1
