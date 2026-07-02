"""AltRescue Stage 2 直餵版 — fastpath miss 後掃 STT 備選救糊字點歌。

背景：top-1 拼音偏離過大（ratio<80）掉出 MusicFastPath 時，SpeechAnalyzer
的備選常藏著更正確的音（gate 判準 2026-07-02：50.7% 有 ≥4 漢字長備選、
音樂意圖句 9/10）。同音字已被拼音比對吃掉——這裡救的是「錯音」。

資料源：engine._last_alt_segments per-speaker 單槽 side-channel
（liveness 同刀 Stage 1，macos_stt_v2 per-segment [[str]]）。

守門（設計 AltLatticeRescue v2，tests/test_alt_rescue.py 釘住）：
  G1 意圖前置閘：stripped 須含真點歌前綴——閒聊/控制指令不進場
  G2 歌單片語不搶單（PersonalShuffleAgent 劫走事故 2026-06-30）
  G3 side-channel 驗證：strip_wake(slot.raw)==stripped，舊句 lattice 不掛新 query
  G4 備選門檻 ≥85（比 top-1 的 80 嚴——猜測性命中要求更高信心）
  G5 kill-switch MARVIN_ALT_RESCUE：0（預設）/shadow（只 log）/on

命中後行為（on 模式）：直接播不反問——誤點抹除按鈕當 undo 兜底。
"""
from __future__ import annotations

import logging
import os
import re

from music_fastpath import strip_command_prefix, _is_playlist_command

logger = logging.getLogger(__name__)

MIN_ALT_HANZI = 4           # gate 統計口徑：≥4 漢字才是歌名級備選
STRICT_SCORE = 85.0         # G4：比 top-1 門檻 80 嚴

_CJK = re.compile(r"[一-鿿]")


def rescue_mode() -> str:
    """kill-switch：'0'（關，預設）/'shadow'（只 log 不動作）/'on'。"""
    v = os.getenv("MARVIN_ALT_RESCUE", "0").strip().lower()
    return v if v in ("shadow", "on") else "0"


def _hanzi_len(s: str) -> int:
    return len(_CJK.findall(s))


def try_alt_rescue(fp, stripped: str, slot, *, strip_wake_fn,
                   min_hanzi: int = MIN_ALT_HANZI,
                   min_score: float = STRICT_SCORE):
    """top-1 fastpath miss 後掃備選 → (result | None, reason)。

    result = {"canonical", "score", "video_id", "alt"}；reason 供 shadow log
    觀察各守門的攔截分佈（no_music_prefix / playlist_phrase / slot_mismatch /
    no_slot / no_alt_hit / hit）。
    """
    # G1 意圖前置閘：剝不出點歌前綴 = 不是點歌句
    song_part = strip_command_prefix(stripped)
    if song_part == stripped:
        return None, "no_music_prefix"
    # G2 歌單指令片語 → PersonalShuffleAgent 的地盤
    if _is_playlist_command(stripped):
        return None, "playlist_phrase"
    # G3 side-channel 驗證
    if not slot:
        return None, "no_slot"
    raw_text, alt_segments, _ts = slot
    if strip_wake_fn(raw_text or "").strip() != stripped.strip():
        return None, "slot_mismatch"
    if not alt_segments:
        return None, "no_alt_segments"

    # 候選：raw 備選 ≥min_hanzi（gate 統計口徑）→ 剝喚醒詞+點歌前綴 → 去重、
    # 排除 top-1 已試過的 song_part
    seen: set[str] = set()
    best = None
    for seg in alt_segments:
        for alt in seg:
            if _hanzi_len(alt) < min_hanzi:
                continue
            cand = strip_command_prefix(strip_wake_fn(alt).strip())
            if not cand or cand == song_part or cand in seen:
                continue
            seen.add(cand)
            hit = fp.match(cand)
            if hit and hit[1] >= min_score and (best is None or hit[1] > best[1][1]):
                best = (cand, hit)
    if best is None:
        return None, "no_alt_hit"
    cand, (canonical, score, video_id) = best
    return {"canonical": canonical, "score": score,
            "video_id": video_id, "alt": cand}, "hit"


def run_alt_rescue(fp, speaker: str, stripped: str, engine, strip_wake_fn):
    """seam 薄包層：env 模式 + slot 讀取 + shadow/on 分流 + log。

    回傳改寫後的點歌指令（on 模式命中）或 None（其餘一律 fall through）。
    voice_controller 只需一行呼叫（size 棘輪）。
    """
    mode = rescue_mode()
    if mode == "0":
        return None
    slot = getattr(engine, "_last_alt_segments", {}).get(speaker) if engine else None
    result, reason = try_alt_rescue(fp, stripped, slot, strip_wake_fn=strip_wake_fn)
    if result is None:
        if reason not in ("no_music_prefix",):   # 高頻閒聊不刷 log
            logger.info(f"🔀 [AltRescue][{mode}] skip reason={reason} q='{stripped[:24]}'")
        return None
    if mode == "shadow":
        logger.info(f"🎵 [AltRescue][shadow] would_play='{result['canonical']}' "
                    f"({result['score']:.0f}) alt='{result['alt']}' q='{stripped[:24]}'")
        return None
    logger.info(f"🎵 [AltRescue][on] '{stripped[:24]}' → '{result['canonical']}' "
                f"({result['score']:.0f}) via alt='{result['alt']}'")
    from music_fastpath import to_play_command
    return to_play_command(result["canonical"], result["video_id"])
