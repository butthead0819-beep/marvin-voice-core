"""
tests/test_get_puck_client_esp32_edge_mix.py

TDD：cogs/music_cog.py::_get_puck_client() 依 MARVIN_CAR_HARDWARE 分流。

2026-08-20：pi_bt（Pi Zero 2W 車 puck）不再有專屬 client——換歌決策/DJ口白改回跟
家用喇叭共用同一顆 mixer、走 /audio_stream「收音機」模式（見
main_satellite.py::setup_satellite 的 TeeSpeakerOutput 說明），這裡回 None，跟其餘
未接控制平面的硬體零差異。

驗：
(a) MARVIN_CAR_HARDWARE=esp32_edge_mix → 回傳的 client 呼叫 queue_next/crossfade 後，
    process-wide 的 default queue 真的多了對應指令（跟 /car_commands 端點讀到的是
    同一份）。
(b) 未設 MARVIN_CAR_HARDWARE（其餘硬體，如家用 Pi 3B）→ 回 None，零行為改變。
(c) MARVIN_CAR_HARDWARE=pi_bt → 回 None（不再有專屬 client）。
"""
from __future__ import annotations

import pytest

from cogs.music_cog import _get_puck_client
from marvin_voice_core.puck_command_queue import get_default_queue


@pytest.mark.asyncio
async def test_esp32_edge_mix_writes_into_shared_default_queue(monkeypatch):
    monkeypatch.setenv("MARVIN_CAR_HARDWARE", "esp32_edge_mix")
    import marvin_voice_core.puck_command_queue as pcq
    monkeypatch.setattr(pcq, "_default_queue", None)   # 隔離其他測試留下的 singleton 狀態

    client = _get_puck_client()
    assert client is not None
    assert await client.queue_next("https://youtu.be/next") is True
    assert await client.crossfade(duration_s=3.5) is True

    seq, pending = get_default_queue().since(0)
    assert seq == 2
    assert [c["cmd"] for c in pending] == ["queue_next", "crossfade"]


def test_unset_hardware_returns_none(monkeypatch):
    monkeypatch.delenv("MARVIN_CAR_HARDWARE", raising=False)
    assert _get_puck_client() is None


def test_pi_bt_hardware_returns_none(monkeypatch):
    monkeypatch.setenv("MARVIN_CAR_HARDWARE", "pi_bt")
    assert _get_puck_client() is None
