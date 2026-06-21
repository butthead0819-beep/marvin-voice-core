"""B — 逐格出圖（nano-banana）測試。

每篇日記一格：用 character bible 把說話者換成動物 + 核心場景 → 出圖 prompt → 出圖。
出圖 fn 注入式（測試用假的，production 接 nano-banana）。失敗一定降級成佔位，不炸拼版。
"""
from PIL import Image

from diary_comic.parser import DiaryEntry
from diary_comic.panel_gen import (
    build_panel_prompt, generate_panel, generate_panel_cached, cache_key,
)


def _entry():
    return DiaryEntry(
        ts_str="2026-06-20 22:44:15",
        core="陳進文和大肚討論木工和裝潢。",
        speakers=["陳進文", "大肚"],
        aside="",
    )


def test_build_panel_prompt_maps_speakers_to_animals():
    p = build_panel_prompt(_entry())
    assert "beaver" in p.lower()  # 陳進文
    assert "cat" in p.lower()     # 大肚


def test_build_panel_prompt_includes_scene_core():
    p = build_panel_prompt(_entry())
    assert "木工" in p or "裝潢" in p


def test_generate_panel_uses_injected_image_fn():
    sentinel = Image.new("RGB", (64, 64), (1, 2, 3))
    got = generate_panel(_entry(), generate_image_fn=lambda prompt, aspect: sentinel)
    assert got is sentinel


def test_generate_panel_passes_aspect_to_fn():
    seen = {}

    def spy(prompt, aspect):
        seen["aspect"] = aspect
        return Image.new("RGB", (8, 8))

    generate_panel(_entry(), generate_image_fn=spy, aspect="9:16")
    assert seen["aspect"] == "9:16"


def test_generate_panel_falls_back_to_placeholder_on_failure():
    def boom(prompt, aspect):
        raise RuntimeError("nano-banana down")

    img = generate_panel(_entry(), generate_image_fn=boom)
    assert isinstance(img, Image.Image)  # 降級成佔位，不丟例外


def test_generate_panel_placeholder_without_fn():
    img = generate_panel(_entry(), generate_image_fn=None)
    assert isinstance(img, Image.Image)


def test_generate_panel_retries_then_succeeds():
    good = Image.new("RGB", (8, 8))
    calls = {"n": 0}

    def flaky(prompt, aspect):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return good

    out = generate_panel(_entry(), generate_image_fn=flaky, retries=2)
    assert out is good          # 第二次成功 → 不降級
    assert calls["n"] == 2


def test_generate_panel_falls_back_after_exhausting_retries():
    calls = {"n": 0}

    def always_bad(prompt, aspect):
        calls["n"] += 1
        raise RuntimeError("down")

    img = generate_panel(_entry(), generate_image_fn=always_bad, retries=2)
    assert isinstance(img, Image.Image)  # 用光重試 → 佔位
    assert calls["n"] == 3               # 1 + 2 retries


def test_generate_panel_retries_on_non_image_result():
    calls = {"n": 0}

    def returns_junk(prompt, aspect):
        calls["n"] += 1
        return None  # 非圖也算失敗，要重試

    img = generate_panel(_entry(), generate_image_fn=returns_junk, retries=1)
    assert isinstance(img, Image.Image)
    assert calls["n"] == 2


def test_cache_key_stable_and_aspect_sensitive():
    e = _entry()
    assert cache_key(e, "1:1") == cache_key(e, "1:1")
    assert cache_key(e, "1:1") != cache_key(e, "9:16")


def test_cache_key_separates_resolution_variant():
    e = _entry()
    # 低解析度 vs 高解析度 = 不同圖，不能共用快取
    assert cache_key(e, "9:16", variant="nano") != cache_key(e, "9:16", variant="pro2k")


def test_generate_panel_cached_variant_misses_across_resolution(tmp_path):
    calls = {"n": 0}

    def gen(prompt, aspect):
        calls["n"] += 1
        return Image.new("RGB", (10, 10))

    d = str(tmp_path)
    generate_panel_cached(_entry(), generate_image_fn=gen, aspect="9:16", cache_dir=d, variant="nano")
    generate_panel_cached(_entry(), generate_image_fn=gen, aspect="9:16", cache_dir=d, variant="pro2k")
    assert calls["n"] == 2  # 不同解析度版本 → 各自出圖


def test_generate_panel_cached_reuses_after_first(tmp_path):
    calls = {"n": 0}

    def once(prompt, aspect):
        calls["n"] += 1
        return Image.new("RGB", (20, 20), (5, 5, 5))

    d = str(tmp_path)
    generate_panel_cached(_entry(), generate_image_fn=once, aspect="1:1", cache_dir=d)
    out = generate_panel_cached(_entry(), generate_image_fn=once, aspect="1:1", cache_dir=d)
    assert calls["n"] == 1          # 第二次走快取，沒再出圖
    assert out.size == (20, 20)


def test_generate_panel_cached_miss_on_different_aspect(tmp_path):
    calls = {"n": 0}

    def gen(prompt, aspect):
        calls["n"] += 1
        return Image.new("RGB", (10, 10))

    d = str(tmp_path)
    generate_panel_cached(_entry(), generate_image_fn=gen, aspect="1:1", cache_dir=d)
    generate_panel_cached(_entry(), generate_image_fn=gen, aspect="9:16", cache_dir=d)
    assert calls["n"] == 2          # 比例不同 → 不同快取，重出


def test_generate_panel_cached_does_not_cache_failure(tmp_path):
    calls = {"n": 0}

    def boom(prompt, aspect):
        calls["n"] += 1
        raise RuntimeError("x")

    d = str(tmp_path)
    generate_panel_cached(_entry(), generate_image_fn=boom, aspect="1:1", cache_dir=d, retries=0)
    generate_panel_cached(_entry(), generate_image_fn=boom, aspect="1:1", cache_dir=d, retries=0)
    assert calls["n"] == 2          # 失敗不快取，下次重試


def test_build_panel_prompt_includes_shot_direction():
    p = build_panel_prompt(_entry(), shot="dramatic low angle looking up")
    assert "low angle" in p.lower()


def test_generate_panel_passes_shot_into_prompt():
    seen = {}

    def spy(prompt, aspect):
        seen["prompt"] = prompt
        from PIL import Image as _I
        return _I.new("RGB", (8, 8))

    generate_panel(_entry(), generate_image_fn=spy, shot="over-the-shoulder shot")
    assert "over-the-shoulder" in seen["prompt"]


def test_cache_key_separates_shot():
    e = _entry()
    assert cache_key(e, "1:1", shot="low angle") != cache_key(e, "1:1", shot="close-up")


def test_build_panel_prompt_object_only_focuses_on_object():
    p = build_panel_prompt(_entry(), object_only=True)
    assert "still-life" in p.lower() or "object" in p.lower()


def test_build_panel_prompt_hero_keeps_characters():
    p = build_panel_prompt(_entry(), object_only=False)
    assert "beaver" in p.lower()  # 非 object_only 仍是角色場景


def test_cache_key_separates_object_only():
    e = _entry()
    assert cache_key(e, "1:1", object_only=True) != cache_key(e, "1:1", object_only=False)


def test_generate_panel_passes_object_only_into_prompt():
    seen = {}

    def spy(prompt, aspect):
        seen["p"] = prompt
        return Image.new("RGB", (8, 8))

    generate_panel(_entry(), generate_image_fn=spy, object_only=True)
    assert "still-life" in seen["p"].lower() or "object" in seen["p"].lower()
