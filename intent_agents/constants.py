"""Single source of truth for music intent keyword constants.

Consumed by:
  - intent_agents/music_agent_v2.py (declare_intents)
  - cogs/voice_controller.py (class attrs + module-level IBA-T0 aliases)

Types:
  - Ordered keyword lists → tuple (regex alternation depends on stable order)
  - IBA-T0 direct keyword sets → frozenset (substring membership only)
"""
from __future__ import annotations


# ── Class-level keyword families (wake-gated path) ─────────────────────────
# 強訊號：含明確音樂字眼，substring match 即視為點歌意圖
STRONG_PLAY_KW: tuple[str, ...] = (
    "放音樂", "播音樂", "放首歌", "播首歌", "放一首", "播一首",
    "來首", "搜尋歌曲",
    "play music", "play song", "play some",
)
# 弱訊號：通用動作詞，需通過 _query_implies_music_intent gate
WEAK_PLAY_KW: tuple[str, ...] = (
    "播放", "我想聽", "放點", "播點", "幫我找", "幫我放",
)
# 總表供 _extract_music_search_query 使用
MUSIC_PLAY_KW: tuple[str, ...] = STRONG_PLAY_KW + WEAK_PLAY_KW

MUSIC_SKIP_KW: tuple[str, ...] = (
    "換一首", "下一首", "跳過", "換歌", "切歌", "不要這首", "skip",
)
MUSIC_STOP_KW: tuple[str, ...] = (
    "停止播放", "音樂停", "不要播了", "關掉音樂", "停音樂", "音樂關掉",
    "stop music", "stop playing",
)
MUSIC_PAUSE_KW: tuple[str, ...] = (
    "暫停音樂", "暫停一下", "pause",
)
MUSIC_RESUME_KW: tuple[str, ...] = (
    "繼續播", "繼續音樂", "播回來", "resume",
)


# ── IBA-T0 direct keywords (no wake gate, stricter subset) ─────────────────
# 刻意排除「暫停一下」（口語歧義高）和英文 pause/skip/resume（遊戲/工作場合常見）。
# 這四個集合與上方 class-level 集合**故意不同**，反映「無喚醒詞」場景的保守選詞。
MUSIC_DIRECT_SKIP_KW: frozenset[str] = frozenset((
    "換一首", "下一首", "跳過", "換歌", "切歌", "不要這首",
))
MUSIC_DIRECT_STOP_KW: frozenset[str] = frozenset((
    "停止播放", "音樂停", "不要播了", "關掉音樂", "停音樂", "音樂關掉",
))
# 明確包含「播放」才不會被 MUSIC_PLAY_KW 搶走
MUSIC_DIRECT_PAUSE_KW: frozenset[str] = frozenset((
    "暫停音樂", "暫停播放", "pause一下",
))
MUSIC_DIRECT_RESUME_KW: frozenset[str] = frozenset((
    "繼續播", "繼續音樂", "播回來",
))
