"""tests/test_icy_bpm_lookup.py — marvin_voice_core.icy_meta.BpmLookup 的純函式測試。

用會計數呼叫次數的假 read_store 驗每條規則；用 `now` 參數控時間，不 sleep。
"""
from __future__ import annotations

from marvin_voice_core.icy_meta import BpmLookup


class _CountingStore:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.data


def test_none_video_id_does_not_call_read_store():
    store = _CountingStore({})
    lookup = BpmLookup(store, retry_s=10.0)
    assert lookup.get(None, 0.0) is None
    assert store.calls == 0


def test_empty_video_id_does_not_call_read_store():
    store = _CountingStore({})
    lookup = BpmLookup(store, retry_s=10.0)
    assert lookup.get("", 0.0) is None
    assert store.calls == 0


def test_hit_then_no_more_read_store_calls():
    store = _CountingStore({"abc": {"bpm": 120.0, "ts": 1.0}})
    lookup = BpmLookup(store, retry_s=10.0)
    assert lookup.get("abc", 0.0) == 120.0
    assert store.calls == 1
    assert lookup.get("abc", 100.0) == 120.0
    assert store.calls == 1


def test_miss_within_retry_window_does_not_reread():
    store = _CountingStore({})
    lookup = BpmLookup(store, retry_s=10.0)
    assert lookup.get("abc", 0.0) is None
    assert store.calls == 1
    assert lookup.get("abc", 5.0) is None
    assert store.calls == 1


def test_miss_after_retry_window_rereads_and_gets_new_value():
    store = _CountingStore({})
    lookup = BpmLookup(store, retry_s=10.0)
    assert lookup.get("abc", 0.0) is None
    assert store.calls == 1
    store.data = {"abc": {"bpm": 95.0, "ts": 20.0}}
    assert lookup.get("abc", 15.0) == 95.0
    assert store.calls == 2


def test_read_store_exception_returns_none():
    def boom():
        raise RuntimeError("boom")

    lookup = BpmLookup(boom, retry_s=10.0)
    assert lookup.get("abc", 0.0) is None


def test_bad_data_shapes_return_none():
    lookup1 = BpmLookup(_CountingStore({"abc": "not-a-dict"}), retry_s=10.0)
    assert lookup1.get("abc", 0.0) is None

    lookup2 = BpmLookup(_CountingStore({"abc": {"bpm": "not-a-number"}}), retry_s=10.0)
    assert lookup2.get("abc", 0.0) is None

    lookup3 = BpmLookup(_CountingStore({"abc": {"bpm": 0}}), retry_s=10.0)
    assert lookup3.get("abc", 0.0) is None

    lookup4 = BpmLookup(_CountingStore({"abc": {"bpm": -5}}), retry_s=10.0)
    assert lookup4.get("abc", 0.0) is None

    lookup5 = BpmLookup(_CountingStore({}), retry_s=10.0)
    assert lookup5.get("missing", 0.0) is None
