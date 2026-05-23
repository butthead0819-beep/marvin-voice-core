"""Wake followup pending state — Marvin 主動問完，user 同一輪回話不需重新喚醒。

純函式 matcher：pending state + raw_text → 合成 wake 句（讓 caller 重投 wake 流程）或 None。
voice_controller 持有 per-speaker pending state，這層只負責「是否該合成 + 合成什麼」。
"""
from __future__ import annotations

from wake_followup import match_followup, is_expired


def _signal(text: str) -> bool:
    """測試用：模擬 wake_intent_gate.has_intent_signal。"""
    return len((text or "").strip()) > 0 and (text or "").strip() not in {"嗯", "啊", "對啊", "喔"}


# ── 無 pending → None ───────────────────────────────────────────────────────

def test_no_pending_returns_none():
    assert match_followup(None, "周杰倫的夜曲", 100.0, 12.0, _signal) is None


# ── 視窗內 + 有訊號 → 合成 wake ──────────────────────────────────────────────

def test_music_song_title_synthesizes():
    pending = {"type": "music_song_title", "original_query": "播首歌", "ts": 100.0}
    result = match_followup(pending, "周杰倫的夜曲", 105.0, 12.0, _signal)
    assert result == "馬文，播周杰倫的夜曲"


def test_music_artist_synthesizes():
    pending = {"type": "music_artist", "original_query": "我想聽歌", "ts": 100.0}
    result = match_followup(pending, "張學友", 105.0, 12.0, _signal)
    assert result == "馬文，播張學友的歌"


def test_generic_type_uses_default_synthesis():
    """未知 type → 通用合成（保守 fallback，未來新 caller 加 type 即可）。"""
    pending = {"type": "any_unknown", "original_query": "你說什麼", "ts": 100.0}
    result = match_followup(pending, "今天天氣很好", 105.0, 12.0, _signal)
    assert result == "馬文，今天天氣很好"


# ── 視窗內 + 純 filler → None（pending 保留，caller 不該清）─────────────────

def test_filler_in_window_returns_none():
    pending = {"type": "music_song_title", "original_query": "播首歌", "ts": 100.0}
    result = match_followup(pending, "嗯", 105.0, 12.0, _signal)
    assert result is None


def test_empty_text_returns_none():
    pending = {"type": "music_song_title", "original_query": "播首歌", "ts": 100.0}
    assert match_followup(pending, "", 105.0, 12.0, _signal) is None
    assert match_followup(pending, "   ", 105.0, 12.0, _signal) is None


# ── 視窗外 → None（caller 用 is_expired 判定清不清）────────────────────────

def test_expired_returns_none():
    pending = {"type": "music_song_title", "original_query": "播首歌", "ts": 100.0}
    # 12s window，13s 後已 expired
    result = match_followup(pending, "周杰倫的夜曲", 113.0, 12.0, _signal)
    assert result is None


def test_at_exact_window_edge_expired():
    """剛好等於 window → 視同 expired（>= 比較）。"""
    pending = {"type": "music_song_title", "ts": 100.0}
    assert match_followup(pending, "周杰倫", 112.0, 12.0, _signal) is None


# ── is_expired helper ─────────────────────────────────────────────────────

def test_is_expired_basic():
    pending = {"type": "x", "ts": 100.0}
    assert is_expired(pending, 113.0, 12.0) is True
    assert is_expired(pending, 110.0, 12.0) is False
    assert is_expired(pending, 112.0, 12.0) is True  # 邊界 = expired


def test_is_expired_no_pending():
    assert is_expired(None, 110.0, 12.0) is False
