"""歌曲卡 embed accent 顏色＝抽出的主色（palette[0]）。"""
import discord

from cogs.voice_views import build_song_embed


def test_embed_color_from_palette_primary():
    e = build_song_embed({"title": "x", "palette": ["#204080", "#F0C040"]})
    assert e.color.value == 0x204080


def test_embed_color_default_without_palette():
    e = build_song_embed({"title": "x"})
    assert e.color == discord.Color.blurple()


def test_embed_color_ignores_bad_hex():
    e = build_song_embed({"title": "x", "palette": ["not-a-hex"]})
    assert e.color == discord.Color.blurple()
