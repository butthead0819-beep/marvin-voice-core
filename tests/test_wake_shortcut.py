"""TDD: wake 後 fastpath 入隊前短路（2026-07-03 晚間現行犯）。

問題：fastpath 掛在 worker 內（dequeue 後）→ 音樂指令排在同 speaker 前一句
聊天回覆（晚間 LLM 降級可卡 60s+）後面，26s 被 Stale Drop 丟掉——fastpath
0ms 能答的事連被問到的機會都沒有。wakeless T0 路徑早就是佇列外直派（7s 播歌），
wake 路徑卻要排隊＝結構不對稱。

shortcut_query(fp, stripped) → 改寫後指令 | None：
  - 歌表命中 → to_play_command（同 fastpath_play_query）
  - 控制指令 → normalize_command
  - 都不是（聊天/問句/空）→ None（照走 worker，確認流不受影響）
"""
from __future__ import annotations

from wake_shortcut import shortcut_query


class _StubFP:
    def __init__(self, hits):
        self._hits = hits

    def match(self, query):
        return self._hits.get(query)


def test_music_hit_returns_play_command():
    fp = _StubFP({"播放陶喆的愛很簡單": ("陶喆 愛很簡單", 95.0, "vidX")})
    out = shortcut_query(fp, "播放陶喆的愛很簡單")
    assert out is not None
    assert "vidX" in out or "陶喆" in out


def test_control_command_normalized():
    out = shortcut_query(_StubFP({}), "下一手")
    assert out == "下一首"


def test_chatter_returns_none():
    assert shortcut_query(_StubFP({}), "今天天氣如何") is None


def test_empty_and_no_fp_safe():
    assert shortcut_query(_StubFP({}), "") is None
    assert shortcut_query(None, "播放晴天") is None


def test_wake_only_returns_none():
    """只喊「馬文」→ stripped 空 → None → 走 worker 等問句（確認流不受影響）。"""
    assert shortcut_query(_StubFP({}), "") is None
