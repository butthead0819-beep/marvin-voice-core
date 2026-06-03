"""TDD — Sentinel DAVE 寬限期盲點修復（2026-06-04 CryptoError 風暴 incident）。

舊邏輯：剛重連 30s 內一律忽略解密錯誤。連線不穩時 connection_time 一直被 reset，
寬限期把「持續零成功解密」的 CryptoError 風暴永久靜音 → dave_error_count 永遠到不了 3
→ 升級（soft-repair → 物理重啟）永不觸發。逼使用者手動重啟。

修法：只在「金鑰真的在同步」時豁免——連線後 early 緩衝(15s)內，或連線後已成功解密過。
若已過 15s 卻自連線以來零成功解密 → 是真壞、不是同步延遲 → 不豁免，讓錯誤累積升級。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _vc_class():
    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
    return VoiceController


def test_early_window_forgives_regardless_of_decrypt():
    """連線後 early 緩衝(15s)內 → 一律豁免（給金鑰同步時間），即使還沒解密過。"""
    VC = _vc_class()
    now = 1000.0
    # 連線 5s 前、自連線以來零解密（last_decrypted 在 connection 之前）
    assert VC._dave_grace_should_forgive(now, connection_time=now - 5, last_decrypted_audio_time=now - 50) is True


def test_in_grace_with_decrypt_forgives():
    """寬限期內(15-30s)且連線後已成功解密過 → 豁免（金鑰正常，只是偶發錯）。"""
    VC = _vc_class()
    now = 1000.0
    # 連線 20s 前、連線後有解密過（last_decrypted 晚於 connection）
    assert VC._dave_grace_should_forgive(now, connection_time=now - 20, last_decrypted_audio_time=now - 3) is True


def test_in_grace_zero_decrypt_does_not_forgive():
    """關鍵修復：寬限期內(過15s)但自連線以來零成功解密 → 真壞，不豁免。"""
    VC = _vc_class()
    now = 1000.0
    # 連線 20s 前、零解密（last_decrypted 早於 connection_time）
    assert VC._dave_grace_should_forgive(now, connection_time=now - 20, last_decrypted_audio_time=now - 25) is False


def test_out_of_grace_does_not_forgive():
    """已出寬限期(>30s) → 正常計數，不豁免。"""
    VC = _vc_class()
    now = 1000.0
    assert VC._dave_grace_should_forgive(now, connection_time=now - 40, last_decrypted_audio_time=now - 3) is False
