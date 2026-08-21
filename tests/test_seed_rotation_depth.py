"""TDD: Autopilot 種子輪替深度與游標滾動測試

問題：
1. 單人/車載模式下，order_rotating_seeds 的 cursor 固定從 0 開始，導致所有 epoch 永遠回傳前 3 首歌。
2. get_played_seed_ids 硬性截斷 20 首，導致候選庫太淺。
"""
from __future__ import annotations
import pytest
from seed_rotation import order_rotating_seeds


def test_single_member_seeds_scroll_through_pool_across_epochs():
    """單人模式下，種子應隨 epoch 滾動遍歷整個 pool，而不是永遠鎖定前 3 首。"""
    members = ["狗與露"]
    pool = [f"seed_{i}" for i in range(12)]
    seeds_by_member = {"狗與露": pool}

    # epoch 0 -> seed 0, 1, 2
    r0 = order_rotating_seeds(members, seeds_by_member, epoch=0, since_manual=10, last_seed=None, n=3)
    assert r0 == ["seed_0", "seed_1", "seed_2"]

    # epoch 1 -> seed 3, 4, 5
    r1 = order_rotating_seeds(members, seeds_by_member, epoch=1, since_manual=10, last_seed=None, n=3)
    assert r1 == ["seed_3", "seed_4", "seed_5"]

    # epoch 2 -> seed 6, 7, 8
    r2 = order_rotating_seeds(members, seeds_by_member, epoch=2, since_manual=10, last_seed=None, n=3)
    assert r2 == ["seed_6", "seed_7", "seed_8"]

    # epoch 3 -> seed 9, 10, 11
    r3 = order_rotating_seeds(members, seeds_by_member, epoch=3, since_manual=10, last_seed=None, n=3)
    assert r3 == ["seed_9", "seed_10", "seed_11"]

    # epoch 4 -> 循環繞回 seed 0, 1, 2
    r4 = order_rotating_seeds(members, seeds_by_member, epoch=4, since_manual=10, last_seed=None, n=3)
    assert r4 == ["seed_0", "seed_1", "seed_2"]


def test_multi_member_seeds_scroll_without_starving_subsequent_seeds():
    """多人模式下，主種子者與次要者在多輪後也能推進各自的種子池。"""
    members = ["Alice", "Bob"]
    seeds_by_member = {
        "Alice": [f"a_{i}" for i in range(10)],
        "Bob": [f"b_{i}" for i in range(10)],
    }

    # epoch 0: 主=Alice (Alice 貢獻 2, Bob 貢獻 1)
    r0 = order_rotating_seeds(members, seeds_by_member, epoch=0, since_manual=10, last_seed=None, n=3, swap_every=1)
    assert "a_0" in r0 and "b_0" in r0

    # epoch 1: 主=Bob (Bob 貢獻 2, Alice 貢獻 1)
    r1 = order_rotating_seeds(members, seeds_by_member, epoch=1, since_manual=10, last_seed=None, n=3, swap_every=1)
    assert "b_1" in r1 or "b_2" in r1 or "b_0" in r1

    # 經過數輪後，應能涵蓋到後續索引（例如 a_3, b_3 等）
    covered = set()
    for ep in range(6):
        res = order_rotating_seeds(members, seeds_by_member, epoch=ep, since_manual=10, last_seed=None, n=3, swap_every=1)
        covered.update(res)

    assert len(covered) >= 6, f"6 輪應該能涵蓋至少 6 種不同種子，實際涵蓋: {covered}"


def test_get_played_seed_ids_default_limit():
    """music_memory.get_played_seed_ids 預設上限應提升至 50 首。"""
    from music_memory import MusicMemory
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        mm = MusicMemory(tmp.name)
        # 構造 40 首不同歌曲
        songs = {}
        for i in range(40):
            url = f"https://www.youtube.com/watch?v=vid_{i:07d}"
            songs[url] = {
                "title": f"Song {i}",
                "requesters": {"狗與露": 100 - i},
            }
        mm._data = {"songs": songs}

        seeds = mm.get_played_seed_ids(["狗與露"])
        assert len(seeds) == 40, f"40 首歷史應該全數被納入種子庫，實際: {len(seeds)}"
