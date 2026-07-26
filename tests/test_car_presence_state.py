"""
tests/test_car_presence_state.py
TDD：car_presence_state.py — 跨進程橋接檔（main_satellite.py 定期寫、music_cog.py 讀）。

比照 now_playing_state.py 同一套模式：純檔案讀寫，無網路無 Discord。
"""
from car_presence_state import is_car_actively_in_use, save_car_presence_state


def test_present_and_fresh_is_actively_in_use(tmp_path):
    path = str(tmp_path / "car_presence_state.json")
    save_car_presence_state(present=True, updated_at=1000.0, path=path)
    assert is_car_actively_in_use(now=1010.0, path=path) is True


def test_present_but_stale_is_not_actively_in_use(tmp_path):
    path = str(tmp_path / "car_presence_state.json")
    save_car_presence_state(present=True, updated_at=1000.0, path=path)
    assert is_car_actively_in_use(now=1000.0 + 200.0, path=path, stale_after_s=90.0) is False


def test_absent_is_not_actively_in_use_even_if_fresh(tmp_path):
    path = str(tmp_path / "car_presence_state.json")
    save_car_presence_state(present=False, updated_at=1000.0, path=path)
    assert is_car_actively_in_use(now=1000.5, path=path) is False


def test_missing_file_is_not_actively_in_use(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    assert is_car_actively_in_use(now=1000.0, path=path) is False
