"""誤喚醒 proxy 的 harvest 窗口回歸測試（2026-06-04 診斷）。

背景：false-wake proxy（discord_voice_engine `_check_false_wake`）用「Track-B 喚醒後
harvest < 5 字」判誤觸，但舊參數 wait=1.1s / after=1.0 太緊：真實後續命令是「說出於
_ts+~2.5s、STT 再延遲 2-3s 才落地」，整段被窗口錯過 → 合法召喚被誤標 false（實測
showay 3 筆 false 有 2 筆當下其實在 active 對話）。這個誤判還會餵 fusion.record_outcome
(False) 反向調高該說話者門檻，讓 bot 越來越不理愛短召喚的人。

修法：harvest 窗口 after 放寬到能涵蓋 _ts+~3s 的後續發言。此測試鎖住窗口行為。
"""
from __future__ import annotations

import time

from discord_voice_engine import ConversationBuffer

# 與 _check_false_wake 對齊的窗口參數（修正後）
HARVEST_BEFORE = 3.0
HARVEST_AFTER = 3.0


def _buf_with_followup(wake_t):
    """模擬：wake_t 有人喚醒（wake-check 不入 buffer），wake_t+2.5 同人講出後續命令。

    時間戳用真實 now 為基準，否則 ConversationBuffer._prune 會把古早 ts 當過期剪掉。
    """
    buf = ConversationBuffer(max_minutes=6)
    buf.add_entry("showay", "沒有爛東西", timestamp=wake_t + 2.5)  # 後續命令，_ts+2.5s
    return buf


def test_followup_at_2p5s_captured_by_widened_window():
    """+2.5s 的後續發言必須落在新窗口內 → harvest 非空 → 不判 false。"""
    wake_t = time.time()
    buf = _buf_with_followup(wake_t)
    harvest = buf.get_harvest(wake_t, before=HARVEST_BEFORE, after=HARVEST_AFTER)
    assert harvest.strip() == "沒有爛東西"
    assert len(harvest.strip()) >= 5          # 不會被判 empty_harvest


def test_followup_missed_by_old_narrow_window():
    """舊窗口 after=1.0 會漏掉 +2.5s 的後續 → 這正是誤標 false 的成因（防退化）。"""
    wake_t = time.time()
    buf = _buf_with_followup(wake_t)
    harvest_old = buf.get_harvest(wake_t, before=3.0, after=1.0)
    assert harvest_old.strip() == ""          # 舊窗口確實抓不到 → 舊版會誤判 false


def test_truly_empty_window_still_flags_false():
    """周圍真的沒有任何發言（如 00:07:00 那筆）→ 仍然空 → 仍判 false（保留真誤觸偵測）。"""
    wake_t = time.time()
    buf = ConversationBuffer(max_minutes=6)
    harvest = buf.get_harvest(wake_t, before=HARVEST_BEFORE, after=HARVEST_AFTER)
    assert harvest.strip() == ""
