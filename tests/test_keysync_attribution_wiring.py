"""KeySync 解密歸因接線：確認 _synced_decrypt_rtp 的觀測 log 內容正確、且不改變既有例外行為。

純觀測：歸因 log 只能多印、不能改變解密路徑的回傳值 / 例外 / 升級判斷。
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from nacl.exceptions import CryptoError


@pytest.fixture(autouse=True)
def _reset_attrib_state():
    """_ATTRIB 是模組層狀態，跨測試會殘留 → 每個測試開頭清空。"""
    import discord_voice_engine
    discord_voice_engine._ATTRIB = None
    yield


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


def _packet(ssrc=4679, data_len=100):
    p = MagicMock()
    p.ssrc = ssrc
    p.header = b"\x80" * 12
    p.data = b"x" * data_len
    return p


def _fresh_decryptor():
    fresh = MagicMock()
    fresh.mode = "aead_xchacha20_poly1305_rtpsize"
    fresh.decrypt_rtp.side_effect = CryptoError("still bad")
    return fresh


def test_storm_emits_attribution_before_escalation(caplog):
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, reader = _make_vc()
    vc._ssrc_to_id = {4679: 999}
    on_storm = MagicMock()
    clock = {"t": 1000.0}
    fake_time = MagicMock()
    fake_time.time.side_effect = lambda: clock["t"]

    with caplog.at_level(logging.WARNING), \
            patch("discord_voice_engine.time", fake_time), \
            patch("discord.ext.voice_recv.reader.PacketDecryptor", side_effect=lambda *a: _fresh_decryptor()):
        patch_voice_recv_key_sync(vc, on_desync_storm=on_storm)
        rtp = reader.decryptor.decrypt_rtp
        for i in range(12):   # 每包前進 1 秒：第 10 包起 ≥10 次且跨度 ≥8 秒
            clock["t"] = 1000.0 + i
            with pytest.raises(CryptoError):
                rtp(_packet())

    on_storm.assert_called()
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    attrib_idx = next(i for i, m in enumerate(msgs) if "🔬 [KeySync] 解密歸因" in m)
    storm_idx = next(i for i, m in enumerate(msgs) if "持續零解密" in m)
    assert attrib_idx < storm_idx
    assert "ssrc=4679" in msgs[attrib_idx]
    assert "uid=999" in msgs[attrib_idx]


def test_meta_failure_keeps_cryptoerror_behavior():
    """_packet_meta 取 len 炸例外 → 解密行為與未加觀測時一致：仍 raise CryptoError。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, reader = _make_vc()
    p = _packet()
    p.header = object()   # len(object()) → TypeError，只能在 _packet_meta 內被吞掉

    with patch("discord.ext.voice_recv.reader.PacketDecryptor", side_effect=lambda *a: _fresh_decryptor()):
        patch_voice_recv_key_sync(vc)
        with pytest.raises(CryptoError) as excinfo:
            reader.decryptor.decrypt_rtp(p)

    assert excinfo.type is CryptoError


def test_meta_snapshot_taken_before_orig_rewrites_packet(caplog):
    """快照時機：orig 解密途中把 packet.data 改短，emit 的 len= 仍是改短之前的長度。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, reader = _make_vc()
    vc._ssrc_to_id = {4679: 999}

    def _shorten_then_fail(p):
        p.data = b"x" * 10   # 模擬 voice_recv rtpsize 解密就地改寫
        raise CryptoError("bad key")

    reader.decryptor.decrypt_rtp.side_effect = _shorten_then_fail
    on_storm = MagicMock()
    clock = {"t": 1000.0}
    fake_time = MagicMock()
    fake_time.time.side_effect = lambda: clock["t"]

    with caplog.at_level(logging.WARNING), \
            patch("discord_voice_engine.time", fake_time), \
            patch("discord.ext.voice_recv.reader.PacketDecryptor", side_effect=lambda *a: _fresh_decryptor()):
        patch_voice_recv_key_sync(vc, on_desync_storm=on_storm)
        rtp = reader.decryptor.decrypt_rtp
        for i in range(12):
            clock["t"] = 1000.0 + i
            with pytest.raises(CryptoError):
                rtp(_packet(data_len=100))   # header 12 + data 100 = 112

    attrib = [r.getMessage() for r in caplog.records if "🔬 [KeySync] 解密歸因" in r.getMessage()]
    assert attrib, "應有歸因 log"
    fail_part = attrib[0].split(" fail=")[1]
    assert fail_part.startswith("10(")   # 第 10 次失敗觸發升級 → force 吐出，共 10 筆 fail
    assert "len=112-112" in fail_part
    assert "len=22" not in attrib[0]   # 22 = 12 + 10，是改寫之後的長度，不該出現
    # 快照 = 改寫前 header(12×0x80) + data 前 4 bytes("x"=0x78) 共 16 bytes
    assert "hdr=80808080808080808080808078787878" in fail_part


def test_second_patch_reuses_same_attrib_across_reconnect():
    """重連時會再次 patch（新 decryptor）；歸因統計必須是同一個物件，異常前基準才留得住。"""
    import discord_voice_engine
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, reader = _make_vc()
    with patch("discord.ext.voice_recv.reader.PacketDecryptor", side_effect=lambda *a: _fresh_decryptor()):
        patch_voice_recv_key_sync(vc)
        first = discord_voice_engine._ATTRIB
        assert first is not None

        new_decryptor = MagicMock()   # 模擬重連後 reader 換了未 patched 的 decryptor
        new_decryptor._key_sync_patched = False
        reader.decryptor = new_decryptor
        patch_voice_recv_key_sync(vc)

    assert discord_voice_engine._ATTRIB is first
    assert new_decryptor._key_sync_patched is True
