"""B — 攝影分鏡：每格指定鏡頭角度，整頁有張力起伏。"""
from diary_comic.camera import shot_for


def test_shot_for_hero_is_dramatic_low_angle():
    s = shot_for(index=2, total=5, is_hero=True)
    assert "low angle" in s.lower()  # 英雄格戲劇性仰拍


def test_shot_for_first_panel_is_establishing():
    s = shot_for(index=0, total=5, is_hero=False)
    assert "establish" in s.lower()  # 第一格建立場景


def test_shot_for_varies_across_non_hero_panels():
    shots = {shot_for(i, total=6, is_hero=False) for i in range(1, 6)}
    assert len(shots) >= 3  # 中段鏡頭要有變化，不能全一樣


def test_shot_for_always_returns_nonempty():
    for i in range(8):
        assert shot_for(i, total=8, is_hero=(i == 3)).strip()


def test_shot_pool_has_extreme_closeup_and_silhouette():
    from diary_comic.camera import _SHOTS
    pool = " ".join(_SHOTS).lower()
    assert "extreme close-up" in pool
    assert "silhouette" in pool


def test_shot_for_varies_widely_in_long_page():
    shots = {shot_for(i, total=8, is_hero=False) for i in range(1, 8)}
    assert len(shots) >= 5  # 8 格頁鏡頭要夠多變


# ---- 三距離節奏：遠景/中景/特寫交替，避免每格證件照 ----
def _distance(shot):
    s = shot.lower()
    if "wide" in s or "establish" in s:
        return "W"
    if "close" in s:
        return "C"
    return "M"


def test_first_panel_is_wide_establishing():
    assert _distance(shot_for(0, 6, is_hero=False)) == "W"  # 第一格遠景交代環境


def test_rhythm_no_three_same_distance_in_a_row():
    ds = [_distance(shot_for(i, 8, is_hero=False)) for i in range(8)]
    assert not any(ds[i] == ds[i + 1] == ds[i + 2] for i in range(len(ds) - 2))  # 不連三同距
    assert len(set(ds)) >= 2  # 至少兩種距離（不全證件照）


def test_rhythm_uses_all_three_distances_over_a_page():
    ds = {_distance(shot_for(i, 8, is_hero=False)) for i in range(8)}
    assert ds == {"W", "M", "C"}  # 遠中特都用到


def test_hero_is_emotional_closeup():
    assert "close" in shot_for(2, 5, is_hero=True).lower()  # 高潮用特寫傳達情緒
