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


def test_bundle_is_self_contained(tmp_path):
    """bundle 版：圖片 base64 內嵌（JPEG 壓縮），零外部引用——可直接私訊分享。"""
    from PIL import Image
    from scripts.build_comic_gallery import build_bundle
    img = Image.new("RGB", (2000, 3000), (30, 30, 40))
    img.save(tmp_path / "diary_comic_20260704_005541.png")
    out = tmp_path / "bundle.html"
    build_bundle(records_dir=tmp_path, out_path=out, max_width=1080, jpeg_q=85)
    html = out.read_text(encoding="utf-8")
    assert "data:image/jpeg;base64," in html
    assert "records/" not in html          # 零外部路徑
    assert "src=\"diary" not in html


def test_bundle_skips_test_artifacts_too(tmp_path):
    from PIL import Image
    from scripts.build_comic_gallery import build_bundle
    Image.new("RGB", (100, 100)).save(tmp_path / "diary_comic_TEST_20260622_235630.png")
    Image.new("RGB", (100, 100)).save(tmp_path / "diary_comic_20260622_010536.png")
    out = tmp_path / "b.html"
    build_bundle(records_dir=tmp_path, out_path=out)
    assert out.read_text(encoding="utf-8").count("data:image/jpeg") == 1
