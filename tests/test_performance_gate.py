"""TDD: 主動表演兩道修正（2026-07-04 使用者定性「錯誤的行為+未爆彈」）。

實錘：22:45:46 主動表演開火、22:45:48 才 BOT降臨——回台瞬間就急著表演；
Suno 掛掉滾 Lyria 備援 → GOOGLE_API_KEY（老專案）429 limit:0 死路，
且 Lyria 無 guard 無記帳（違反付費記帳鐵則）。

  G1 summon/回台後寬限期內不主動表演（讓人先講話）
  G2 Lyria 備援 env-gated 預設關（未過 guard 的付費路不准活）；
     Suno 失敗 → 優雅放棄，不滾備援
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


def test_lyria_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MARVIN_LYRIA", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaFakeKey")
    from music_engine import SukiMusicEngine as MusicEngine
    eng = MusicEngine(api_key="AIzaFakeKey")
    assert eng.lyria_client is None       # 預設不建 client＝零呼叫


def test_lyria_retired_even_with_env(monkeypatch):
    # 2026-07-05 使用者決策：Lyria 永久退役，env 設 1 也不得復活
    monkeypatch.setenv("MARVIN_LYRIA", "1")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaFakeKey")
    from music_engine import SukiMusicEngine as MusicEngine, lyria_enabled
    assert lyria_enabled() is False
    eng = MusicEngine(api_key="AIzaFakeKey")
    assert eng.lyria_client is None
