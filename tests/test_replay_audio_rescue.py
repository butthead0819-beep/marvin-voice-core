"""scripts/replay_audio_rescue.py 的非-Gemini 部分（manifest 組裝、resolve 卡關
歸因、CLI 守衛）。真正打 Gemini 的整條鏈屬 T8 整合測試 / 人工 replay。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("GOOGLE_API_KEY", "test-key")  # 過 key 守衛才 import 得到

_SPEC = importlib.util.spec_from_file_location(
    "replay_audio_rescue",
    Path(__file__).resolve().parent.parent / "scripts" / "replay_audio_rescue.py",
)
replay = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(replay)


def test_manifest_only_exposes_opt_in_intents():
    from intent_agents.audio_rescue_tools import manifest_to_function_declarations
    agents = replay._build_agents()
    decls = manifest_to_function_declarations(replay._manifest(agents))
    names = {d.name for d in decls}
    assert "music__rescue_play" in names
    assert "music__strong_play" not in names  # 8 個 regex schema 不曝
    assert names >= {
        "grounded_qa__factual_question", "volume__volume_down",
        "playback_control__skip_track", "find_song__find_artist",
    }


def _audio_ctx():
    from intent_bus import IntentContext
    return IntentContext(
        speaker="r", raw_text="", query="", original_raw="", wake_intent=0.9,
        stream_active=True, game_mode=False, is_owner=False, now=0.0, mode="normal",
        dispatch_source="llm_rescue_audio",
    )


def test_why_resolve_none_reports_missing_slot():
    """find_song 沒有 post_match_filter override → 缺 slot 卡在 missing 這關。"""
    from intent_agents.find_song_agent import FindSongAgent

    agent = FindSongAgent(SimpleNamespace())
    assert replay._why_resolve_none(agent, "find_artist", {}, _audio_ctx()) == "missing_slot:artist"


def test_why_resolve_none_reports_post_match_filter():
    """rescue_play 的 post_match_filter 檢查 song_query 非空，先於 missing 檢查。"""
    from intent_agents.music_agent_v2 import MusicAgentV2

    agent = MusicAgentV2(SimpleNamespace())
    assert replay._why_resolve_none(agent, "rescue_play", {}, _audio_ctx()) == "post_match_filter"


def test_why_resolve_none_reports_gate():
    from intent_agents.volume_agent import VolumeAgent
    from intent_bus import IntentContext

    agent = VolumeAgent(SimpleNamespace(stream_mode=False, radio_mode=False))
    ctx = IntentContext(
        speaker="r", raw_text="", query="", original_raw="", wake_intent=0.9,
        stream_active=False, game_mode=False, is_owner=False, now=0.0, mode="normal",
        dispatch_source="llm_rescue_audio",
    )
    assert replay._why_resolve_none(agent, "volume_down", {}, ctx) == "gate:no_playback_active"


def test_missing_wav_dir_exits(tmp_path):
    import asyncio
    with pytest.raises(SystemExit):
        asyncio.run(replay._main(tmp_path / "nope", as_json=False))
