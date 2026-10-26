"""tests/test_puck_skin.py — puck_skin.py::SkinStore 的純 I/O 測試。

驗：put 驗證長度/id、etag 穩定性、clear、current() 1 秒快取、底圖檔被刪時的
退化行為、read_current() 不快取且回正確 bytes。全部用 tmp_path。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os

import pytest

from puck_skin import SKIN_BYTES, SkinStore


def _data(byte: int = 0xAB) -> bytes:
    return bytes([byte]) * SKIN_BYTES


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_put_wrong_length_raises_value_error(tmp_path):
    store = SkinStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("a", b"\x00" * (SKIN_BYTES - 1))


def test_put_bad_id_raises_value_error(tmp_path):
    store = SkinStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("bad id!", _data())


def test_put_id_too_long_raises_value_error(tmp_path):
    store = SkinStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("x" * 41, _data())


def test_put_then_current_returns_id_and_etag(tmp_path):
    store = SkinStore(tmp_path)
    data = _data()
    etag = store.put("winter", data)
    assert len(etag) == 8
    int(etag, 16)  # 8 hex chars
    assert etag == hashlib.sha1(data).hexdigest()[:8]
    assert store.current() == ("winter", etag)


def test_same_data_gives_same_etag(tmp_path):
    store = SkinStore(tmp_path)
    data = _data(0x11)
    etag1 = store.put("a", data)
    etag2 = store.put("b", data)
    assert etag1 == etag2


def test_clear_then_current_is_none(tmp_path):
    store = SkinStore(tmp_path)
    store.put("winter", _data())
    store.clear()
    assert store.current() is None


def test_clear_when_nothing_set_does_not_raise(tmp_path):
    store = SkinStore(tmp_path)
    store.clear()


def test_current_is_cached_for_one_second(tmp_path):
    clock = _FakeClock()
    store = SkinStore(tmp_path, clock=clock)
    data_winter = _data(0x11)
    etag_winter = store.put("winter", data_winter)

    first = store.current()
    assert first == ("winter", etag_winter)

    # 繞過 SkinStore.put()（它會清快取）直接改底層檔案，模擬「快取期內資料被換掉」
    data_summer = _data(0x22)
    etag_summer = hashlib.sha1(data_summer).hexdigest()[:8]
    (tmp_path / "summer.rgb").write_bytes(data_summer)
    tmp_json = tmp_path / "current.json.tmp"
    tmp_json.write_text(json.dumps({"id": "summer", "etag": etag_summer}), encoding="utf-8")
    os.replace(tmp_json, tmp_path / "current.json")

    clock.advance(0.5)
    assert store.current() == first  # 仍在 1 秒快取內，回舊值

    clock.advance(0.6)  # 總共 1.1s > 1.0s TTL
    assert store.current() == ("summer", etag_summer)


def test_current_none_when_rgb_file_deleted(tmp_path):
    store = SkinStore(tmp_path)
    store.put("winter", _data())
    (tmp_path / "winter.rgb").unlink()
    assert store.current() is None


def test_current_none_when_current_json_missing(tmp_path):
    store = SkinStore(tmp_path)
    assert store.current() is None


def test_current_none_when_current_json_corrupt(tmp_path):
    store = SkinStore(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "current.json").write_text("not json", encoding="utf-8")
    assert store.current() is None


def test_read_current_returns_etag_and_bytes(tmp_path):
    store = SkinStore(tmp_path)
    data = _data(0x33)
    etag = store.put("winter", data)
    result = store.read_current()
    assert result == (etag, data)


def test_read_current_none_when_nothing_set(tmp_path):
    store = SkinStore(tmp_path)
    assert store.read_current() is None


def test_read_current_not_cached(tmp_path):
    store = SkinStore(tmp_path)
    store.put("winter", _data(0x44))
    store.put("summer", _data(0x55))
    etag, data = store.read_current()
    assert data == _data(0x55)


def test_seasonal_current_when_in_range_and_file_exists(tmp_path):
    data = _data(0x11)
    (tmp_path / "x.rgb").write_bytes(data)
    store = SkinStore(
        tmp_path,
        today=lambda: datetime.date(2026, 10, 15),
        seasonal=[("10-01", "10-31", "x")],
    )
    expected_etag = hashlib.sha1(data).hexdigest()[:8]
    assert store.current() == ("x", expected_etag)
    assert store.read_current() == (expected_etag, data)


def test_seasonal_current_none_when_out_of_range(tmp_path):
    (tmp_path / "x.rgb").write_bytes(_data(0x11))
    store = SkinStore(
        tmp_path,
        today=lambda: datetime.date(2026, 11, 1),
        seasonal=[("10-01", "10-31", "x")],
    )
    assert store.current() is None


def test_seasonal_current_boundary_start_and_end_dates_hit(tmp_path):
    (tmp_path / "x.rgb").write_bytes(_data(0x11))

    store_start = SkinStore(
        tmp_path,
        today=lambda: datetime.date(2026, 10, 1),
        seasonal=[("10-01", "10-31", "x")],
    )
    assert store_start.current() is not None

    store_end = SkinStore(
        tmp_path,
        today=lambda: datetime.date(2026, 10, 31),
        seasonal=[("10-01", "10-31", "x")],
    )
    assert store_end.current() is not None


def test_manual_skin_takes_priority_over_seasonal_then_clear_falls_back(tmp_path):
    (tmp_path / "x.rgb").write_bytes(_data(0x11))
    store = SkinStore(
        tmp_path,
        today=lambda: datetime.date(2026, 10, 15),
        seasonal=[("10-01", "10-31", "x")],
    )
    store.put("manual", _data(0x22))
    assert store.current()[0] == "manual"

    store.clear()
    assert store.current()[0] == "x"


def test_seasonal_current_none_when_skin_file_missing(tmp_path):
    store = SkinStore(
        tmp_path,
        today=lambda: datetime.date(2026, 10, 15),
        seasonal=[("10-01", "10-31", "x")],
    )
    assert store.current() is None


def test_seasonal_current_none_when_skin_file_wrong_length(tmp_path):
    (tmp_path / "x.rgb").write_bytes(b"\x00" * (SKIN_BYTES - 1))
    store = SkinStore(
        tmp_path,
        today=lambda: datetime.date(2026, 10, 15),
        seasonal=[("10-01", "10-31", "x")],
    )
    assert store.current() is None


def test_seasonal_current_cross_year_range(tmp_path):
    (tmp_path / "x.rgb").write_bytes(_data(0x11))
    seasonal = [("12-25", "01-05", "x")]

    store_jan2 = SkinStore(tmp_path, today=lambda: datetime.date(2027, 1, 2), seasonal=seasonal)
    assert store_jan2.current() is not None

    store_dec30 = SkinStore(tmp_path, today=lambda: datetime.date(2026, 12, 30), seasonal=seasonal)
    assert store_dec30.current() is not None

    store_jan6 = SkinStore(tmp_path, today=lambda: datetime.date(2027, 1, 6), seasonal=seasonal)
    assert store_jan6.current() is None


def test_seasonal_current_uses_module_default_when_not_overridden(tmp_path):
    (tmp_path / "halloween_pumpkin.rgb").write_bytes(_data(0x11))
    store = SkinStore(tmp_path, today=lambda: datetime.date(2026, 10, 25))
    assert store.current()[0] == "halloween_pumpkin"
