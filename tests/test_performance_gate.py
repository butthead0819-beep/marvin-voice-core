"""TDD: 主動表演修正（2026-07-04 使用者定性「錯誤的行為」）。

實錘：22:45:46 主動表演開火、22:45:48 才 BOT降臨——回台瞬間就急著表演。

  G1 summon/回台後寬限期內不主動表演（讓人先講話）
"""
from __future__ import annotations

from cogs.voice_controller_social import too_soon_after_summon


def test_gate_blocks_right_after_summon():
    assert too_soon_after_summon(connection_time=1000.0, now=1030.0) is True   # 30s
    assert too_soon_after_summon(connection_time=1000.0, now=1000.0 + 599) is True


def test_gate_opens_after_grace():
    assert too_soon_after_summon(connection_time=1000.0, now=1000.0 + 601) is False


def test_gate_failopen_without_connection_time():
    assert too_soon_after_summon(connection_time=0, now=1030.0) is False
    assert too_soon_after_summon(connection_time=None, now=1030.0) is False

