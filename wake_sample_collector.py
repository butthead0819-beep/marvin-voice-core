"""Opt-in 喚醒音檔收集器——存 owner 喊「馬文」的真實 wav，供之後訓 openWakeWord 自訓模型。

STT 暫存 wav 每次用完就刪（設計如此）；此收集器在**刪除前**把「owner + raw 含喚醒詞」
的那段複製到 records/wake_samples/（+ sidecar json 存 raw_text/ts）。累積真人、真聲學、
真音樂背景的「馬文」樣本＝訓練/評估自訓喚醒模型最理想的資料。

守門（全過才存）：
  - env `MARVIN_COLLECT_WAKE_WAV=1` 才收（**預設關**）
  - 只 owner（`MARVIN_OWNER_ID`，預設既有 id）——隱私：只存本人喚醒段
  - raw STT 文字含喚醒詞才存（非喚醒的隨口話不收）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from utils import check_cleaned_text_for_wake

logger = logging.getLogger(__name__)

_DIR = Path("records/wake_samples")


def _owner_id() -> int:
    try:
        return int(os.getenv("MARVIN_OWNER_ID", "876758076831723580"))
    except ValueError:
        return 0


def collect(wav_path: str | None, user_id: int | None, raw_text: str | None) -> None:
    """opt-in 存喚醒 wav。env off / 非 owner / raw 無喚醒詞 / 檔不存在 → no-op（安全）。"""
    if os.getenv("MARVIN_COLLECT_WAKE_WAV") != "1":
        return
    if user_id is None or user_id != _owner_id():
        return
    if not check_cleaned_text_for_wake(raw_text or ""):
        return
    if not wav_path or not os.path.exists(wav_path):
        return
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        stem = f"owner_{int(ts)}_{int((ts % 1) * 1_000_000):06d}"
        shutil.copy(wav_path, _DIR / f"{stem}.wav")
        (_DIR / f"{stem}.json").write_text(
            json.dumps({"ts": ts, "user_id": user_id, "raw": raw_text}, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(f"📼 [WakeSample] 已存喚醒樣本 {stem}.wav（raw='{(raw_text or '')[:30]}'）")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"⚠️ [WakeSample] 存檔失敗: {e}")
