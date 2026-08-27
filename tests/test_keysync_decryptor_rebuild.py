"""KeySync：重抓 key 仍解不開時，用當前 mode 整個重建 decryptor（零音訊中斷）。

背景（2026-08-27）：voice_recv 的 PacketDecryptor.update_secret_key() 只換 box、
不換 self.mode 也不重綁 decrypt_rtp/rtcp。reconnect 後 decryptor 綁死已失效 session
或協商到不同 mode 時，光重讀 key 永遠解不開，只有重新 listen()（Sentinel 完整軟修復）
才會用當前 mode 重建 → 中間整段爆音 + STT 全失效。

修法：patch_voice_recv_key_sync 在「重讀 key 後仍 CryptoError」時，先試 _rebuild_decryptor
用 voice_client.mode + secret_key 重建 reader.decryptor，成功就地復原、免整條重連。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from nacl.exceptions import CryptoError


def _make_vc():
    vc = MagicMock()
    vc.secret_key = bytes(32)
    vc.mode = "aead_xchacha20_poly1305_rtpsize"
    vc._ssrc_to_id = {}
    reader = MagicMock()
    decryptor = MagicMock()
    decryptor._key_sync_patched = False
    decryptor.decrypt_rtp.side_effect = CryptoError("bad key")   # 舊 decryptor 永遠壞
    decryptor.decrypt_rtcp.side_effect = CryptoError("bad key")
    reader.decryptor = decryptor
    vc._reader = reader
    state = MagicMock()
    state.dave_ready = False
    vc._connection = state
    return vc, reader


def _packet(ssrc=4679):
    p = MagicMock()
    p.ssrc = ssrc
    return p


def test_persistent_cryptoerror_rebuilds_decryptor_with_current_mode():
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, reader = _make_vc()

    fresh = MagicMock()
    fresh.mode = vc.mode
    fresh.decrypt_rtp.return_value = b"GOOD_OPUS"

    with patch("discord.ext.voice_recv.reader.PacketDecryptor", return_value=fresh) as PD:
        patch_voice_recv_key_sync(vc)
        out = reader.decryptor.decrypt_rtp(_packet())

    PD.assert_called_once_with(vc.mode, bytes(vc.secret_key))
    assert reader.decryptor is fresh          # decryptor 被換掉
    assert out == b"GOOD_OPUS"                 # 就地復原、沒拋 CryptoError


def test_rebuild_is_debounced_within_5s():
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, reader = _make_vc()
    fresh = MagicMock()
    fresh.mode = vc.mode
    fresh.decrypt_rtp.side_effect = CryptoError("still bad")   # 重建後也壞

    with patch("discord.ext.voice_recv.reader.PacketDecryptor", return_value=fresh) as PD:
        patched_rtp = None
        patch_voice_recv_key_sync(vc)
        patched_rtp = reader.decryptor.decrypt_rtp
        for _ in range(5):
            with pytest.raises(CryptoError):
                patched_rtp(_packet())

    assert PD.call_count == 1   # 5s 內狂丟壞封包只重建一次
