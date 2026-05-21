"""TDD: no-wake 點歌（IBA-T0）改走 IntentBus（Level A consolidation）。

修 directional/curation 字串在 no-wake 路徑被直送 yt-dlp 搜出垃圾的 bug
（2026-05-21 prod 實測「播放周杰倫符合我年紀的歌」搜到 Eric周興哲）。

build_nowake_play_ctx 把 _extract_music_search_query 剝乾淨的 query 重建成
MusicAgentV2 認得的指令句，並設 wake_intent=None 關掉 guard Track-B 規則。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from intent_agents.music_agent_v2 import MusicAgentV2
from cogs.voice_controller import build_nowake_play_ctx


def test_prepends_play_kw_and_nulls_wake_intent():
    ctx = build_nowake_play_ctx(
        "大肚", "馬文播放周杰倫符合我年紀的歌", "周杰倫符合我年紀的歌",
        stream_active=False, is_owner=False,
    )
    assert ctx.query == "播放周杰倫符合我年紀的歌"
    assert ctx.wake_intent is None       # 關掉 HallucinationGuard Track-B 短query規則
    assert ctx.speaker == "大肚"
    assert ctx.original_raw == "馬文播放周杰倫符合我年紀的歌"


def test_does_not_double_prefix_play():
    ctx = build_nowake_play_ctx("x", "raw", "播放周杰倫", stream_active=False, is_owner=False)
    assert ctx.query == "播放周杰倫"


def test_song_title_starting_with_fang_still_prefixed():
    """歌名以「放」開頭（放牛班的春天）不該被誤判已有 kw → 仍要前綴「播放」。"""
    ctx = build_nowake_play_ctx("x", "raw", "放牛班的春天", stream_active=False, is_owner=False)
    assert ctx.query == "播放放牛班的春天"


# ── 重建的 query 餵 MusicAgentV2 → 命中正確的檔（端到端接點）──────────────────

def test_reconstructed_directional_bids_directional():
    ctx = build_nowake_play_ctx("x", "raw", "周杰倫符合我年紀的歌",
                                stream_active=False, is_owner=False)
    bid = MusicAgentV2(MagicMock()).bid(ctx)
    assert bid.confidence == 0.50
    assert bid.missing_slots == ["directional_resolution"]


def test_reconstructed_artist_only_bids_curation():
    ctx = build_nowake_play_ctx("x", "raw", "周杰倫", stream_active=False, is_owner=False)
    bid = MusicAgentV2(MagicMock()).bid(ctx)
    assert bid.confidence == 0.85
    assert bid.missing_slots == ["song_choice"]


def test_reconstructed_specific_bids_specific():
    ctx = build_nowake_play_ctx("x", "raw", "周杰倫的稻香", stream_active=False, is_owner=False)
    bid = MusicAgentV2(MagicMock()).bid(ctx)
    assert bid.confidence == 0.95
    assert bid.missing_slots == []
