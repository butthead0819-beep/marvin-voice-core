"""Device 記憶對齊：satellite 要讀寫**正本**記憶、且落在跟 Discord 同一個
per-person 分區 (GUILD_ID, speaker)。這裡測純驗證函式 check_identity_alignment。

背景（見 project_marvin_physical_speaker「2026-07-07 記憶/靈魂對齊」）：
- 記憶檔是相對 cwd 路徑 → device 要跑在 repo 根目錄才讀正本（非 worktree 各自一份）
- per-person 記憶 PK=(GUILD_ID, username)：GUILD_ID 要跟 Discord 同、speaker 要映射到既有身分
"""
import os

from main_satellite import check_identity_alignment, repo_root


def test_aligned_env_has_no_warnings():
    env = {"GUILD_ID": "1234567890123456789", "MARVIN_SATELLITE_SPEAKER": "狗與露"}
    assert check_identity_alignment(env) == []


def test_missing_guild_id_warns():
    env = {"MARVIN_SATELLITE_SPEAKER": "狗與露"}
    w = check_identity_alignment(env)
    assert any("GUILD_ID" in x for x in w)


def test_zero_guild_id_warns():
    env = {"GUILD_ID": "0", "MARVIN_SATELLITE_SPEAKER": "狗與露"}
    w = check_identity_alignment(env)
    assert any("GUILD_ID" in x for x in w)


def test_missing_speaker_warns():
    env = {"GUILD_ID": "1234567890123456789"}
    w = check_identity_alignment(env)
    assert any("SPEAKER" in x or "身分" in x for x in w)


def test_repo_root_is_this_repo():
    # 錨定點必須是含 main_satellite.py 的 repo 根目錄
    assert os.path.isfile(os.path.join(repo_root(), "main_satellite.py"))
    assert os.path.isfile(os.path.join(repo_root(), "main_discord.py"))
