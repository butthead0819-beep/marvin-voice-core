"""TDD: 漫畫合集週更派送（2026-07-04 使用者拍板：打包+每週主動更新+私訊每人一次）。

派送規則：
  R1 content_key = 漫畫檔名集合的穩定 hash——只有「新漫畫出現」才算新版
     （像素級重壓不觸發重送）
  R2 每人每版只送一次（state 記 user→content_key；重跑/補跑安全）
  R3 收件人 = consent.json consented==true 的成員（隱私邊界與語音同一套）
  R4 成員名解析：display_name/nick/global_name 任一吻合
"""
from __future__ import annotations

import json

from scripts.comic_bundle_weekly import (content_key, load_state, match_member,
                                         should_send, mark_sent)


def test_content_key_stable_and_order_free():
    a = content_key(["diary_comic_b.png", "diary_comic_a.png"])
    b = content_key(["diary_comic_a.png", "diary_comic_b.png"])
    assert a == b and len(a) == 12


def test_content_key_changes_with_new_comic():
    assert content_key(["a.png"]) != content_key(["a.png", "b.png"])


def test_should_send_new_version(tmp_path):
    st = tmp_path / "state.json"
    assert should_send(load_state(st), "u1", "key1") is True


def test_should_send_once_per_version(tmp_path):
    st = tmp_path / "state.json"
    s = load_state(st)
    mark_sent(s, "u1", "key1", path=st)
    s2 = load_state(st)
    assert should_send(s2, "u1", "key1") is False   # 同版不重送
    assert should_send(s2, "u1", "key2") is True    # 新版要送
    assert should_send(s2, "u2", "key1") is True    # 別人還沒收過


def test_match_member_by_any_name_field():
    members = [
        {"user": {"id": "111", "username": "gogolu", "global_name": "狗與露"}, "nick": None},
        {"user": {"id": "222", "username": "showay", "global_name": None}, "nick": "showay"},
    ]
    assert match_member(members, "狗與露")["user"]["id"] == "111"
    assert match_member(members, "showay")["user"]["id"] == "222"
    assert match_member(members, "路人") is None


# ── v2：圖片原生派送（HTML 在 Discord 變下載附件，改送可預覽的圖） ──────────

def test_chunk_batches_of_ten():
    from scripts.comic_bundle_weekly import chunk
    items = list(range(23))
    batches = chunk(items, 10)
    assert [len(b) for b in batches] == [10, 10, 3]


def test_format_version_forces_resend(tmp_path):
    """HTML 版已送過的人，切到 img 版 key 改變 → 重送一次圖片版。"""
    from scripts.comic_bundle_weekly import content_key, should_send, load_state, mark_sent
    st = tmp_path / "s.json"
    s = load_state(st)
    old_key = content_key(["a.png"])                    # 無前綴（HTML 時代）
    mark_sent(s, "u1", old_key, path=st)
    new_key = content_key(["a.png"], fmt="img1")
    assert new_key != old_key
    assert should_send(load_state(st), "u1", new_key) is True


def test_new_comics_only_delta(tmp_path):
    """週更只送新增的：state 記 last_names，delta = 現有 − 已送過的檔名。"""
    from scripts.comic_bundle_weekly import compute_delta
    state = {"last_names": ["a.png", "b.png"]}
    assert compute_delta(state, ["a.png", "b.png", "c.png"]) == ["c.png"]
    assert compute_delta({}, ["a.png"]) == ["a.png"]      # 首次全送
