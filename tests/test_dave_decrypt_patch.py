"""DAVE E2EE decrypt 接入 voice_recv 的單元測試。

背景: 5/22 Discord 在 guild 啟用 DAVE 後，audio packet 是 SRTP+DAVE 雙層加密。
voice_recv 0.5.2a179 只解開外層 SRTP，內層 DAVE 不處理，opus payload 還是密文 → STT 全 0。

修正: patch_voice_recv_key_sync 在 SRTP decrypt 後，若 voice_state.dave_ready 為 True，
就呼叫 voice_client._connection.dave_session.decrypt(uid, MediaType.audio, plaintext)
取得真正的 opus bytes。
"""
from __future__ import annotations

import logging

from unittest.mock import MagicMock

import pytest

from nacl.exceptions import CryptoError


def _make_voice_client(*, dave_ready: bool, ssrc_map: dict[int, int] | None = None):
    """Build a mock voice_client with the minimum surface patch needs."""
    vc = MagicMock()
    vc.secret_key = bytes(32)
    vc._ssrc_to_id = ssrc_map or {}

    # voice_recv reader + decryptor
    reader = MagicMock()
    decryptor = MagicMock()
    decryptor._key_sync_patched = False  # 讓 patch 跑一次
    reader.decryptor = decryptor
    vc._reader = reader

    # discord.py voice_state — dave_session / dave_ready 屬性
    state = MagicMock()
    state.dave_ready = dave_ready
    state.dave_session = MagicMock()
    state.dave_session.decrypt = MagicMock(return_value=b"DAVE_PLAINTEXT_OPUS")
    vc._connection = state

    return vc, decryptor, state


def _make_packet(ssrc: int = 4679):
    pkt = MagicMock()
    pkt.ssrc = ssrc
    return pkt


# ---------------------------------------------------------------------------
# Sanity: patch 永遠掛載且不會重複
# ---------------------------------------------------------------------------

def test_patch_marks_key_sync_patched_idempotent():
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, _ = _make_voice_client(dave_ready=False)
    patch_voice_recv_key_sync(vc)
    assert decryptor._key_sync_patched is True

    # 二次呼叫不該再包一層
    first_fn = decryptor.decrypt_rtp
    patch_voice_recv_key_sync(vc)
    assert decryptor.decrypt_rtp is first_fn


# ---------------------------------------------------------------------------
# DAVE OFF 路徑：行為與舊版相同（SRTP-only）
# ---------------------------------------------------------------------------

def test_dave_off_returns_srtp_plaintext_directly():
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, state = _make_voice_client(dave_ready=False, ssrc_map={4679: 12345})
    srtp_plain = b"SRTP_PLAIN"
    decryptor.decrypt_rtp.return_value = srtp_plain
    # 抓 patch 前的原 callable，因為 patch 會包它
    orig_decrypt = decryptor.decrypt_rtp

    patch_voice_recv_key_sync(vc)

    pkt = _make_packet(ssrc=4679)
    result = decryptor.decrypt_rtp(pkt)
    assert result == srtp_plain
    orig_decrypt.assert_called_once_with(pkt)
    state.dave_session.decrypt.assert_not_called()


# ---------------------------------------------------------------------------
# DAVE ON：SRTP plaintext → davey.decrypt → 真正的 opus
# ---------------------------------------------------------------------------

def test_dave_on_runs_davey_decrypt_with_user_id_from_ssrc_map():
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, state = _make_voice_client(dave_ready=True, ssrc_map={4679: 876758076831723580})
    decryptor.decrypt_rtp.return_value = b"SRTP_PLAIN_DAVE_CIPHERTEXT"

    patch_voice_recv_key_sync(vc)

    pkt = _make_packet(ssrc=4679)
    result = decryptor.decrypt_rtp(pkt)

    # davey 該被呼叫，user_id 從 _ssrc_to_id 拿
    state.dave_session.decrypt.assert_called_once()
    call_args = state.dave_session.decrypt.call_args
    assert call_args.args[0] == 876758076831723580  # user_id
    assert call_args.args[2] == b"SRTP_PLAIN_DAVE_CIPHERTEXT"  # SRTP plaintext

    # davey decrypt 的結果是 final
    assert result == b"DAVE_PLAINTEXT_OPUS"


def test_dave_on_passthrough_when_ssrc_unmapped():
    """unknown ssrc (使用者剛進來) 不該炸；先回 SRTP plaintext，等 ssrc map 更新。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, state = _make_voice_client(dave_ready=True, ssrc_map={})
    srtp_plain = b"SRTP_PLAIN_NO_UID"
    decryptor.decrypt_rtp.return_value = srtp_plain

    patch_voice_recv_key_sync(vc)

    pkt = _make_packet(ssrc=99999)
    result = decryptor.decrypt_rtp(pkt)

    # 沒 uid → 跳過 davey，回 SRTP plaintext
    state.dave_session.decrypt.assert_not_called()
    assert result == srtp_plain


def test_dave_on_davey_exception_fallback_to_srtp_plaintext():
    """davey.decrypt 內部錯（passthrough transition / wrong epoch）不該丟掉整個 packet。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, state = _make_voice_client(dave_ready=True, ssrc_map={4679: 12345})
    srtp_plain = b"SRTP_PLAIN_DAVE_FAILED"
    decryptor.decrypt_rtp.return_value = srtp_plain
    state.dave_session.decrypt.side_effect = RuntimeError("epoch transition")

    patch_voice_recv_key_sync(vc)

    pkt = _make_packet(ssrc=4679)
    result = decryptor.decrypt_rtp(pkt)

    # davey 試過但失敗 → 回 SRTP plaintext（passthrough 模式可能就剛好是明文）
    state.dave_session.decrypt.assert_called_once()
    assert result == srtp_plain


# ---------------------------------------------------------------------------
# State 不存在的 defensive paths
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# KeySync retry：malformed 封包噪音抑制（2026-06-01）
#
# 背景：RTCP/unknown-ssrc 雜散封包進 RTP 解密 → 第一次 CryptoError → keysync 重試
# → 解密算出負 buffer 長度 → ValueError("negative array length")。原本這個非
# CryptoError 例外往上拋，被 library 的 `except Exception: log.exception(...)` 噴整個
# traceback（289/天）。reader.py 對 CryptoError 是乾淨單行 drop，對其他 Exception
# 才噴 traceback —— 故重試失敗一律轉 CryptoError，讓 library 走乾淨分支。
# ---------------------------------------------------------------------------

def test_malformed_packet_retry_reraises_as_cryptoerror():
    """重試炸非 CryptoError（malformed）→ 轉成 CryptoError 上拋，讓 library 乾淨 drop。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, _ = _make_voice_client(dave_ready=False)
    # 第一次 CryptoError（被誤判成換 key）→ 重試炸 ValueError（負陣列長度）
    decryptor.decrypt_rtp.side_effect = [
        CryptoError("Decryption failed."),
        ValueError("negative array length"),
    ]
    patch_voice_recv_key_sync(vc)

    pkt = _make_packet()
    # 對外是 CryptoError（不是 ValueError）→ reader.py 走 log.error 單行 + return
    with pytest.raises(CryptoError):
        decryptor.decrypt_rtp(pkt)


def test_malformed_packet_no_warning_log(caplog):
    """malformed 封包不再噴 WARNING（降 DEBUG），避免噪音洗版。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, _ = _make_voice_client(dave_ready=False)
    decryptor.decrypt_rtp.side_effect = [
        CryptoError("Decryption failed."),
        ValueError("negative array length"),
    ]
    patch_voice_recv_key_sync(vc)

    pkt = _make_packet()
    with caplog.at_level(logging.WARNING, logger="MarvinBot.Engine"):
        with pytest.raises(CryptoError):
            decryptor.decrypt_rtp(pkt)
    assert not any("同步失敗" in r.message for r in caplog.records), \
        "malformed 封包不該再噴 WARNING（應降 DEBUG）"


def test_keysync_retry_success_returns_decrypted():
    """真 key 過期：第一次 CryptoError → 重抓 key 重試成功 → 回 plaintext（保留既有復原）。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, _ = _make_voice_client(dave_ready=False)
    decryptor.decrypt_rtp.side_effect = [
        CryptoError("stale key"),
        b"RECOVERED_PLAINTEXT",  # 重試成功
    ]
    patch_voice_recv_key_sync(vc)

    pkt = _make_packet()
    result = decryptor.decrypt_rtp(pkt)
    assert result == b"RECOVERED_PLAINTEXT"
    decryptor.update_secret_key.assert_called_once()


def test_keysync_retry_still_cryptoerror_reraises():
    """重試仍 CryptoError（key 真的沒救）→ 維持拋 CryptoError，library 乾淨 drop。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc, decryptor, _ = _make_voice_client(dave_ready=False)
    decryptor.decrypt_rtp.side_effect = [
        CryptoError("fail 1"),
        CryptoError("fail 2"),
    ]
    patch_voice_recv_key_sync(vc)

    pkt = _make_packet()
    with pytest.raises(CryptoError):
        decryptor.decrypt_rtp(pkt)


def test_no_dave_session_attr_does_not_break():
    """老 discord.py（沒接 DAVE）voice_state 沒有 dave_ready 屬性。"""
    from discord_voice_engine import patch_voice_recv_key_sync

    vc = MagicMock()
    vc.secret_key = bytes(32)
    vc._ssrc_to_id = {4679: 12345}
    reader = MagicMock()
    decryptor = MagicMock()
    decryptor._key_sync_patched = False
    decryptor.decrypt_rtp.return_value = b"OLD_DISCORD_PY"
    reader.decryptor = decryptor
    vc._reader = reader
    # 沒有 _connection 屬性
    del vc._connection

    patch_voice_recv_key_sync(vc)
    pkt = _make_packet()
    result = decryptor.decrypt_rtp(pkt)
    assert result == b"OLD_DISCORD_PY"


# ---------------------------------------------------------------------------
# secret_key desync 風暴自癒：持續零解密 → on_desync_storm callback
# （2026-06-23 incident：傳輸層 CryptoError 風暴 Sentinel 看不到、炸 40 分沒升級）
# ---------------------------------------------------------------------------

def test_sustained_decrypt_storm_triggers_on_desync_storm(monkeypatch):
    """重抓 key 仍持續零解密(RESUME 沿用舊 key) → patch 觸發完整重連自癒 callback。"""
    import discord_voice_engine as dve
    from discord_voice_engine import patch_voice_recv_key_sync

    clock = {"t": 1000.0}
    monkeypatch.setattr(dve.time, "time", lambda: clock["t"])

    vc, decryptor, _ = _make_voice_client(dave_ready=False)
    decryptor.decrypt_rtp.side_effect = CryptoError("desync")  # 永遠解不開

    fired = []
    patch_voice_recv_key_sync(vc, on_desync_storm=lambda: fired.append(1))

    patched = decryptor.decrypt_rtp  # patch 後為 synced 版
    for i in range(15):              # 15 次封包跨 14s 全失敗
        clock["t"] = 1000.0 + i
        try:
            patched(_make_packet())
        except CryptoError:
            pass

    assert fired, "持續零解密應觸發 on_desync_storm 自癒"
    assert len(fired) == 1, "升級只觸發一次、不 spam orchestrate_recovery"


def test_transient_failures_recovered_by_resync_do_not_escalate(monkeypatch):
    """重抓 key 後就解開（瞬間抖動，KeySync 救得回）→ 不該升級完整重連。"""
    import discord_voice_engine as dve
    from discord_voice_engine import patch_voice_recv_key_sync

    clock = {"t": 2000.0}
    monkeypatch.setattr(dve.time, "time", lambda: clock["t"])

    vc, decryptor, _ = _make_voice_client(dave_ready=False)
    # 第一次 raise、重抓 key 後第二次成功 → 每個封包都靠 resync 救回（record_success）
    decryptor.decrypt_rtp.side_effect = [CryptoError("blip"), b"OK"] * 20

    fired = []
    patch_voice_recv_key_sync(vc, on_desync_storm=lambda: fired.append(1))

    patched = decryptor.decrypt_rtp
    for i in range(20):
        clock["t"] = 2000.0 + i
        patched(_make_packet())

    assert not fired, "靠 resync 救回的瞬間抖動不該觸發完整重連"
