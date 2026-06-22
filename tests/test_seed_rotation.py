"""多人種子輪替純函式測試。"""
from seed_rotation import primary_member, order_rotating_seeds

_MEMBERS = ["狗與露", "showay", "大肚"]
_SEEDS = {
    "狗與露": ["jay1", "jay2"],
    "showay": ["tw1", "tw2"],   # 台語傾向
    "大肚": ["da1"],
}


def test_primary_member_rotates_every_swap_every():
    # epoch 0-2 → 第0人；3-5 → 第1人；6-8 → 第2人；9 → 回第0人
    assert primary_member(_MEMBERS, 0) == "狗與露"
    assert primary_member(_MEMBERS, 2) == "狗與露"
    assert primary_member(_MEMBERS, 3) == "showay"
    assert primary_member(_MEMBERS, 6) == "大肚"
    assert primary_member(_MEMBERS, 9) == "狗與露"


def test_fresh_manual_seed_leads_then_fades():
    # 手動點歌後 since_manual<3 → last_seed 領頭
    s = order_rotating_seeds(_MEMBERS, _SEEDS, epoch=0, since_manual=0,
                             last_seed="manual_song")
    assert s[0] == "manual_song"
    # 3 首後（since_manual>=3）→ 不再領頭（淡出）
    s2 = order_rotating_seeds(_MEMBERS, _SEEDS, epoch=0, since_manual=3,
                              last_seed="manual_song")
    assert "manual_song" not in s2


def test_always_blends_other_members():
    # 主種子者是 showay（台語），但其他人也要被混進來 → 不會整串台語
    s = order_rotating_seeds(_MEMBERS, _SEEDS, epoch=3, since_manual=9,
                             last_seed=None, n=3)
    assert s[0] in _SEEDS["showay"]          # 主種子者先
    assert any(v in _SEEDS["狗與露"] for v in s)  # 但有混到狗與露
    assert any(v in _SEEDS["大肚"] for v in s)    # 也混到大肚


def test_no_duplicate_seeds():
    seeds = {"a": ["x"], "b": ["x", "y"]}
    s = order_rotating_seeds(["a", "b"], seeds, epoch=0, since_manual=9, last_seed="x")
    assert len(s) == len(set(s))


def test_empty_members_falls_back_to_last_seed():
    assert order_rotating_seeds([], {}, epoch=0, since_manual=0, last_seed="x") == ["x"]
    assert order_rotating_seeds([], {}, epoch=0, since_manual=0, last_seed=None) == []
