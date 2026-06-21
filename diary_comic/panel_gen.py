"""逐格出圖：一篇日記 → 一格漫畫（nano-banana）。

用 character bible 把說話者換成動物 + 核心當場景 → 出圖 prompt → 出圖。
出圖 fn 注入式（generate_image_fn(prompt)->PIL.Image），production 接 nano-banana，
測試接假的。任何失敗都降級成佔位圖，不炸整條拼版（CLAUDE.md fallback 鐵則）。
"""
from __future__ import annotations

import hashlib
import os
from typing import Callable

from PIL import Image, ImageDraw

from diary_comic.character_store import cast_description, MARVIN
from diary_comic.parser import DiaryEntry

ImageFn = Callable[[str, "str | None"], Image.Image]  # (prompt, aspect) -> Image

_STYLE = (
    "A single comic panel, cute warm flat-illustration style, soft colors, clean border, "
    "with DYNAMIC, cinematic, dramatic composition and strong perspective for tension. "
    "Characters have BIG, exaggerated, varied anime/manga facial expressions that clearly "
    "read the emotion of the moment — wide or squinting eyes, expressive eyebrows, open-mouth "
    "laughs or smirks, plus sweat drops / blush lines / shock lines where they fit. "
    "Give each character a DIFFERENT expression for contrast. "
    "NO text or letters inside the image — tell it through faces, poses and camera angle."
)


_OBJECT_STYLE = (
    "A clean still-life illustration focusing on the main OBJECT / ITEM being discussed "
    "(e.g. the speaker, gadget, instrument, food, blueprint), cute warm flat style, soft "
    "simple background, NO characters (or only a tiny silhouette hint). NO text or letters."
)


def build_panel_prompt(entry: DiaryEntry, shot: str = "", object_only: bool = False) -> str:
    """組單格出圖 prompt。object_only=只畫討論主體（物件），否則畫角色場景。"""
    camera = f"\nCamera angle / shot: {shot}." if shot else ""
    if object_only:
        return f"{_OBJECT_STYLE}\nTopic being discussed: {entry.core}{camera}"
    cast = cast_description(entry.speakers) or "a couple of friendly animal characters"
    return (
        f"{_STYLE}\n"
        f"Characters (keep them consistent across panels): {cast}. "
        f"Optionally {MARVIN.appearance} watching from a corner.\n"
        f"Scene (what they are doing): {entry.core}{camera}"
    )


def _placeholder(entry: DiaryEntry, size: tuple[int, int]) -> Image.Image:
    img = Image.new("RGB", size, (210, 170, 140))
    d = ImageDraw.Draw(img)
    d.text((10, 10), "・".join(entry.speakers) or "(?)", fill=(255, 255, 255))
    d.text((10, 30), entry.core[:16], fill=(255, 255, 240))
    return img


def _try_generate(entry: DiaryEntry, fn: ImageFn, aspect: str | None, retries: int,
                  shot: str = "", object_only: bool = False) -> Image.Image | None:
    """出圖 + 重試。全失敗回 None（讓呼叫端決定降級/不快取）。"""
    prompt = build_panel_prompt(entry, shot, object_only)
    for _attempt in range(retries + 1):
        try:
            img = fn(prompt, aspect)
            if isinstance(img, Image.Image):
                return img
        except Exception:
            pass  # 非圖或例外都當失敗，再試
    return None


def generate_panel(entry: DiaryEntry,
                   generate_image_fn: ImageFn | None = None,
                   aspect: str | None = None,
                   size: tuple[int, int] = (512, 512),
                   retries: int = 2,
                   shot: str = "",
                   object_only: bool = False) -> Image.Image:
    """出一格圖（aspect=比例、shot=鏡頭、object_only=只畫物件）。失敗降級佔位。"""
    if generate_image_fn is None:
        return _placeholder(entry, size)
    img = _try_generate(entry, generate_image_fn, aspect, retries, shot, object_only)
    return img if img is not None else _placeholder(entry, size)


def cache_key(entry: DiaryEntry, aspect: str | None,
              variant: str = "", shot: str = "", object_only: bool = False) -> str:
    """逐格圖片快取 key：同條目+比例+版本+鏡頭+物件模式 → 同 key。改字幕/版面不影響。"""
    raw = f"{entry.ts_str}|{entry.core}|{aspect}|{variant}|{shot}|{int(object_only)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def generate_panel_cached(entry: DiaryEntry,
                          generate_image_fn: ImageFn | None = None,
                          aspect: str | None = None,
                          cache_dir: str | None = None,
                          size: tuple[int, int] = (512, 512),
                          retries: int = 2,
                          variant: str = "",
                          shot: str = "",
                          object_only: bool = False) -> Image.Image:
    """有快取版：圖只出一次存檔，之後重用。失敗不快取。variant/shot/object_only 區分版本。"""
    path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(
            cache_dir, cache_key(entry, aspect, variant, shot, object_only) + ".png")
        if os.path.exists(path):
            try:
                return Image.open(path).convert("RGB")
            except Exception:
                pass  # 壞檔 → 當沒快取，重出
    if generate_image_fn is None:
        return _placeholder(entry, size)
    img = _try_generate(entry, generate_image_fn, aspect, retries, shot, object_only)
    if img is None:
        return _placeholder(entry, size)  # 失敗不快取
    if path:
        try:
            img.save(path)
        except Exception:
            pass
    return img
