"""Marvin 主動問後的同 user 跟回機制（pending followup state）— 純函式 matcher。

Why：MusicAgent 接到 weak_play_long_string 等 missing-slot 路徑 → `_ask_music_followup`
貼「你想聽哪一首？」。user 回答時不該再喊「馬文」才被接到。原本的 Deferred Wake
只管 user 自己起手低信心 wake，這條補另一半（Marvin 主動問 → user 回答）。

設計：
- voice_controller 持有 per-speaker pending state（dict keyed by speaker）
- 這層只判「該不該合成 + 合成什麼」，不管 state lifecycle（caller 自己 set/pop）
- pending state shape: {"type": str, "original_query": str, "ts": float}
- 視窗內收到 filler/無訊號 → 回 None，pending 保留（user 可能還在想）
- 視窗外 → 回 None，caller 用 is_expired() 判定要不要清掉
"""
from __future__ import annotations

from typing import Callable, Optional


def match_followup(
    pending: Optional[dict],
    raw_text: str,
    now: float,
    window_s: float,
    has_signal_fn: Callable[[str], bool],
) -> Optional[str]:
    """有效 pending + raw_text 有訊號 → 回合成 wake 句；否則 None。

    Returns:
        str: 合成的 wake-style 句子（讓 caller 重投 handle_stt_result）
        None: 無 pending / expired / 純 filler — caller 走原路徑
    """
    if not pending:
        return None
    if (now - pending.get("ts", 0.0)) >= window_s:
        return None  # expired
    if not raw_text or not raw_text.strip():
        return None
    if not has_signal_fn(raw_text):
        return None  # 純 filler，pending 留著等

    ptype = pending.get("type", "")
    if ptype == "music_song_title":
        return f"馬文，播{raw_text}"
    if ptype == "music_artist":
        return f"馬文，播{raw_text}的歌"
    # 未知 type → 通用合成，未來新 caller 補 type 即可
    return f"馬文，{raw_text}"


def is_expired(pending: Optional[dict], now: float, window_s: float) -> bool:
    """pending 是否已超過視窗（caller 用來決定 pop state）。"""
    if not pending:
        return False
    return (now - pending.get("ts", 0.0)) >= window_s
