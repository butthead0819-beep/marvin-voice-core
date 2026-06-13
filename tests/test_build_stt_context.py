"""動態 STT contextualStrings 組裝（2026-06-13）。

原本只注入喚醒詞 + 遊戲字典；加：當前/佇列歌名與歌手、活躍講者暱稱——
直接救「這首誰唱的」「播放○○」的專名辨識。

鐵則（CLAUDE.md）：注入的 context 同時是幻覺來源，caller 必須把同一份
字串餵給 is_whisper_hallucination 過濾 echo-back（engine 端已接）。
"""
from __future__ import annotations

from utils import build_stt_context, is_whisper_hallucination


def test_combines_base_game_songs_members():
    ctx = build_stt_context(
        base="Marvin,馬文",
        game_dict="麥塊,鑽石",
        song_pairs=[("晴天", "周杰倫")],
        members=["狗與露", "showay"],
    )
    parts = ctx.split(",")
    for expected in ("Marvin", "馬文", "麥塊", "鑽石", "晴天", "周杰倫", "狗與露", "showay"):
        assert expected in parts


def test_title_noise_tokens_filtered():
    """YouTube 標題垃圾詞（Official/MV/官方/HD…）不該進 context。"""
    ctx = build_stt_context(
        base="馬文", game_dict="",
        song_pairs=[("晴天 Official Music Video 官方完整版 MV HD", "周杰倫 Jay Chou")],
        members=[],
    )
    parts = set(ctx.split(","))
    assert "晴天" in parts
    assert "周杰倫" in parts
    for noise in ("Official", "Music", "Video", "官方完整版", "MV", "HD"):
        assert noise not in parts


def test_dedupes_and_skips_empty():
    ctx = build_stt_context(
        base="馬文,馬文", game_dict="馬文,,  ",
        song_pairs=[("", ""), ("馬文", "")],
        members=["馬文"],
    )
    assert ctx.split(",").count("馬文") == 1


def test_cap_limits_entry_count():
    ctx = build_stt_context(
        base="", game_dict="",
        song_pairs=[(f"歌名{i}", f"歌手{i}") for i in range(100)],
        members=[], cap=30,
    )
    assert len(ctx.split(",")) <= 30


def test_overlong_tokens_dropped():
    """超長 token（整句歌詞式標題殘渣）不注入——context 要的是短專名。"""
    ctx = build_stt_context(
        base="馬文", game_dict="",
        song_pairs=[("憑什麼你回頭我就要在身後需要你的時候你都不在我左右", "馬師傅")],
        members=[],
    )
    parts = set(ctx.split(","))
    assert "馬師傅" in parts
    assert all(len(p) <= 12 for p in parts)


def test_injected_titles_caught_by_hallucination_filter():
    """端到端鐵則驗證：注入的歌名若被 STT 原樣 echo（純 prompt token 組成），
    is_whisper_hallucination 用同一份 context 能抓到。"""
    ctx = build_stt_context(
        base="Marvin,馬文", game_dict="",
        song_pairs=[("晴天", "周杰倫")], members=[],
    )
    assert is_whisper_hallucination("晴天 周杰倫", ctx) is True
    # 正常句夾雜歌名不誤殺
    assert is_whisper_hallucination("幫我播周杰倫的晴天", ctx) is False
