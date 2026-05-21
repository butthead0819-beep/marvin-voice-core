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
