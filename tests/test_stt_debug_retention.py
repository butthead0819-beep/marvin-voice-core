"""stt_debug_*.wav 7 天保留輪替。

只有 writer、無 reader 的除錯落盤，靠檔名內嵌時間戳（非 mtime，shutil.copy 會改 mtime）
判斷是否過期，避免無界累積。
"""
from datetime import datetime

from stt_debug_retention import prune_stt_debug


def _touch(d, name):
    p = d / name
    p.write_bytes(b"\x00")
    return p


def test_prune_removes_files_older_than_retention(tmp_path):
    now = datetime(2026, 7, 13, 12, 0, 0)
    old = _touch(tmp_path, "stt_debug_20260705_120000_20.2s.wav")  # 8 天前
    removed = prune_stt_debug(tmp_path, now=now, retention_days=7)
    assert not old.exists()
    assert removed == [old]


def test_prune_keeps_files_within_retention(tmp_path):
    now = datetime(2026, 7, 13, 12, 0, 0)
    recent = _touch(tmp_path, "stt_debug_20260710_235900_5.0s.wav")  # 3 天前
    removed = prune_stt_debug(tmp_path, now=now, retention_days=7)
    assert recent.exists()
    assert removed == []


def test_prune_ignores_static_shortcut_and_unrelated_files(tmp_path):
    now = datetime(2026, 7, 13, 12, 0, 0)
    static = _touch(tmp_path, "last_stt_debug.wav")
    other = _touch(tmp_path, "suki_voice_deadbeef.mp3")
    prune_stt_debug(tmp_path, now=now, retention_days=7)
    assert static.exists()
    assert other.exists()


def test_prune_skips_unparseable_names_without_crashing(tmp_path):
    now = datetime(2026, 7, 13, 12, 0, 0)
    weird = _touch(tmp_path, "stt_debug_garbage.wav")
    removed = prune_stt_debug(tmp_path, now=now, retention_days=7)
    assert weird.exists()  # 解析不出時間戳 → 保守保留
    assert removed == []


def test_prune_boundary_exactly_at_retention_edge_is_kept(tmp_path):
    now = datetime(2026, 7, 13, 12, 0, 0)
    edge = _touch(tmp_path, "stt_debug_20260706_120000_1.0s.wav")  # 剛好 7 天
    removed = prune_stt_debug(tmp_path, now=now, retention_days=7)
    assert edge.exists()
    assert removed == []
