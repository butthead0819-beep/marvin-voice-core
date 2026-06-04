"""blend_radio_results — 多 seed 的 radio 結果交錯混合 + 去重 + 排除。

round-robin 交錯，讓每個 seed 的口味都進前段（而非單一 seed 灌滿）；跨 seed 去重
(url+title)；exclude_titles 過濾；limit 截斷。
"""
from __future__ import annotations

from ytmusic_radio import blend_radio_results


def test_interleaves_and_dedupes_across_seeds():
    s1 = [{"title": "A", "url": "u1"}, {"title": "B", "url": "u2"}]
    s2 = [{"title": "A", "url": "u1"}, {"title": "C", "url": "u3"}]   # A 跨 seed 重複
    out = blend_radio_results([s1, s2], limit=10)
    assert [c["title"] for c in out] == ["A", "B", "C"]   # 交錯：A、(A dup skip)、B、C


def test_excludes_titles():
    s1 = [{"title": "X", "url": "u1"}, {"title": "Y", "url": "u2"}]
    out = blend_radio_results([s1], exclude_titles=["X"])
    assert [c["title"] for c in out] == ["Y"]


def test_respects_limit():
    s1 = [{"title": f"T{i}", "url": f"u{i}"} for i in range(10)]
    out = blend_radio_results([s1], limit=3)
    assert len(out) == 3


def test_empty_input_returns_empty():
    assert blend_radio_results([]) == []
    assert blend_radio_results([[], []]) == []


def test_skips_entries_missing_url():
    s1 = [{"title": "A", "url": ""}, {"title": "B", "url": "u2"}]
    out = blend_radio_results([s1])
    assert [c["title"] for c in out] == ["B"]
