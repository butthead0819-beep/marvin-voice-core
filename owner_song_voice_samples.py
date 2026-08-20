"""Opt-in 收集 owner 點歌當下的語音片段——DJ 介紹該首歌時可回放原音當彩蛋。

STT 暫存 wav 用完即刪（同 wake_sample_collector.py 設計）；此收集器在刪除前，把
「owner + raw 文字像在點歌」的那段複製到 records/owner_song_voice_samples/
（+ sidecar json 存 raw_text/ts）。短期滾動保留 7 天（每次 collect 順手清過期）。

守門（全過才存）：
  - env `MARVIN_COLLECT_SONG_VOICE_SAMPLES=1` 才收（**預設關**）
  - 只 owner（`MARVIN_OWNER_ID`，跟 wake_sample_collector.py 同 id / 同 fallback）
  - raw STT 文字含點歌關鍵字才存（純聊天不收）

消費端（介紹歌曲時）用 `find_recent_clip(match_text)` 撈「raw_text 跟這首歌標題/
歌手有字元重疊，且在保留窗口內」的最新樣本，回傳 wav 路徑；找不到回 None，呼叫端
退回純 TTS，不影響原行為。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from intent_agents.constants import MUSIC_PLAY_KW

logger = logging.getLogger(__name__)

_DIR = Path("records/owner_song_voice_samples")
_TTL_S = 7 * 24 * 3600  # 短期滾動保留 7 天
_MATCH_WINDOW_S = 30 * 60  # 點歌到 DJ 介紹這首歌通常在半小時內；超過視為不相關


def _owner_id() -> int:
    try:
        return int(os.getenv("MARVIN_OWNER_ID", "876758076831723580"))
    except ValueError:
        return 0


def _looks_like_song_request(raw_text: str) -> bool:
    return any(kw in raw_text for kw in MUSIC_PLAY_KW)


def _prune_expired() -> None:
    if not _DIR.exists():
        return
    cutoff = time.time() - _TTL_S
    for p in _DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                p.with_suffix(".wav").unlink(missing_ok=True)
        except OSError:
            pass


def collect(wav_path: str | None, user_id: int | None, raw_text: str | None) -> None:
    """opt-in 存點歌 wav。env off / 非 owner / 非點歌語句 / 檔不存在 → no-op（安全）。"""
    if os.getenv("MARVIN_COLLECT_SONG_VOICE_SAMPLES") != "1":
        return
    if user_id is None or user_id != _owner_id():
        return
    if not _looks_like_song_request(raw_text or ""):
        return
    if not wav_path or not os.path.exists(wav_path):
        return
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _prune_expired()
        ts = time.time()
        stem = f"owner_{int(ts)}_{int((ts % 1) * 1_000_000):06d}"
        shutil.copy(wav_path, _DIR / f"{stem}.wav")
        (_DIR / f"{stem}.json").write_text(
            json.dumps({"ts": ts, "raw": raw_text}, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(f"🎙️ [SongVoiceSample] 已存點歌樣本 {stem}.wav（raw='{(raw_text or '')[:30]}'）")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"⚠️ [SongVoiceSample] 存檔失敗: {e}")


def find_recent_clip(match_text: str, *, max_age_s: float = _MATCH_WINDOW_S) -> str | None:
    """撈「raw_text 跟 match_text 有字元重疊」且在 max_age_s 內的最新樣本 wav 路徑。

    match_text 通常傳 f"{title} {uploader}"。純字元 overlap 判斷，不追求精準比對
    （中文分詞成本高、樣本量小）——找不到就回 None，呼叫端退回純 TTS 不受影響。
    """
    if not _DIR.exists() or not match_text:
        return None
    now = time.time()
    tokens = {c for c in match_text if not c.isspace()}
    best_path, best_ts = None, -1.0
    for p in _DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ts = data.get("ts", 0.0)
        if now - ts >= max_age_s:
            continue
        raw = data.get("raw", "") or ""
        overlap = sum(1 for c in raw if c in tokens)
        if overlap < 2:  # 至少 2 個字元重疊才算像同一首歌，避免亂配對
            continue
        if ts > best_ts:
            wav_path = p.with_suffix(".wav")
            if wav_path.exists():
                best_path, best_ts = str(wav_path), ts
    return best_path
