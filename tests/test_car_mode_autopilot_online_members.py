"""TDD — 車 puck 佇列播完會停播事故（2026-07-25）。

根因：car 模式沒有 Discord 語音頻道，`vc.get_online_members()` 永遠回 []。
`_autorecommend_seed` 把「Marvin 推薦歌播完 + online=[]」判成空房、正確地不續推
（這個語意本身沒錯，見 test_autorecommend_trigger.py）——但 car 模式的「空 online」
是誤判，puck 心跳明明知道車上有人。修法：`_autopilot_online_members` 在 online 為空
且 MARVIN_CAR_MODE 開啟時，用 MARVIN_SATELLITE_SPEAKER 頂上，讓 autopilot 續推鏈
不會在車上被誤判成空房而斷。
"""
from __future__ import annotations

from cogs.music_cog import MusicCog


def test_home_mode_empty_online_stays_empty(monkeypatch):
    """一般（家用 Discord）模式：語音頻道真的沒人 → 維持 []，交給既有 auto-dismiss。"""
    monkeypatch.delenv("MARVIN_CAR_MODE", raising=False)
    assert MusicCog._autopilot_online_members([]) == []


def test_car_mode_empty_online_falls_back_to_satellite_speaker(monkeypatch):
    """car 模式 + online 空（必然如此，car 沒有 Discord 語音頻道）→ 頂上 puck owner。"""
    monkeypatch.setenv("MARVIN_CAR_MODE", "1")
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", "阿凱")
    assert MusicCog._autopilot_online_members([]) == ["阿凱"]


def test_car_mode_empty_online_default_speaker(monkeypatch):
    """沒設 MARVIN_SATELLITE_SPEAKER → 跟 main_satellite.py 的預設值一致。"""
    monkeypatch.setenv("MARVIN_CAR_MODE", "1")
    monkeypatch.delenv("MARVIN_SATELLITE_SPEAKER", raising=False)
    assert MusicCog._autopilot_online_members([]) == ["狗與露"]


def test_car_mode_nonempty_online_unchanged(monkeypatch):
    """online 非空（理論上不會發生在 car 模式，但函式本身不該亂改真實資料）→ 原樣回傳。"""
    monkeypatch.setenv("MARVIN_CAR_MODE", "1")
    assert MusicCog._autopilot_online_members(["showay"]) == ["showay"]


def test_car_mode_unblocks_autorecommend_seed_chain(monkeypatch):
    """整合驗證：car 模式下 Marvin 推薦歌播完，接上 _autorecommend_seed 仍能續推
    （regression 的完整鏈路，不只測 helper 本身）。"""
    monkeypatch.setenv("MARVIN_CAR_MODE", "1")
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", "狗與露")
    online = MusicCog._autopilot_online_members([])
    seed = MusicCog._autorecommend_seed("Marvin推薦（為狗與露）", online)
    assert seed == "狗與露"
