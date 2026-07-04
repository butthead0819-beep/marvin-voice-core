"""TDD: 漫畫合集頁生成（2026-07-04 使用者要求：未貼出的也收進網頁合集）。"""
from __future__ import annotations

from scripts.build_comic_gallery import collect_comics


def test_collect_parses_and_sorts_newest_first(tmp_path):
    for name in ("diary_comic_20260621_234513.png",
                 "diary_comic_20260704_005541.png",
                 "diary_comic_20260622_010536.png"):
        (tmp_path / name).write_bytes(b"png")
    out = collect_comics(tmp_path)
    assert [e["date"] for e in out] == ["2026/07/04 00:55", "2026/06/22 01:05", "2026/06/21 23:45"]


def test_collect_skips_test_artifacts(tmp_path):
    (tmp_path / "diary_comic_TEST_20260622_235630.png").write_bytes(b"png")
    (tmp_path / "diary_comic_20260622_010536.png").write_bytes(b"png")
    assert len(collect_comics(tmp_path)) == 1


def test_collect_ignores_non_comic_pngs(tmp_path):
    (tmp_path / "night_reel_20260615_2126.png").write_bytes(b"png")
    assert collect_comics(tmp_path) == []
