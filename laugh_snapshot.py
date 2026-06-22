"""笑聲當下快照：同時發聲人數（sink VAD）+ 在場非 bot 人數（voice channel）。

寫進 transcript_store.laugh_events，供 diary_comic 的哄堂比例閘濾掉陪笑。
從 VoiceController.handle_stt_result 呼叫——抽成 module-level 避免 god-object 長新 method。
全防禦、非阻塞：任何失敗都吞掉，絕不影響 STT 主路徑。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from diary_comic.highlight import count_concurrent_voices

logger = logging.getLogger(__name__)

LAUGH_CONCURRENCY_WINDOW = 3.0  # 笑聲當下回看幾秒算「同時發聲」

SAMPLE_DIR = "records/laugh_samples"
SAMPLE_CAP = 500  # 驗證用：最多收幾段，避免塞爆磁碟


def maybe_capture_sample(raw_text, wav_bytes, speaker, timestamp) -> None:
    """[DEBUG, 預設 OFF] 驗證 A：env LAUGH_SAMPLE_CAPTURE=1 時把 WAV 落盤，
    依 STT 文字標 laugh/speech，供離線節律分析。注意：繞過 ZDR 秒刪，只給擁有者驗證用。"""
    if os.getenv("LAUGH_SAMPLE_CAPTURE", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    if not wav_bytes:
        return
    try:
        from diary_comic.highlight import is_laugh, laugh_strength
        os.makedirs(SAMPLE_DIR, exist_ok=True)
        if len(os.listdir(SAMPLE_DIR)) >= SAMPLE_CAP:
            return
        label = f"laugh{laugh_strength(raw_text)}" if is_laugh(raw_text) else "speech"
        safe = "".join(c for c in (speaker or "x") if c.isalnum())[:10] or "x"
        with open(os.path.join(SAMPLE_DIR, f"{label}_{safe}_{timestamp:.0f}.wav"), "wb") as f:
            f.write(wav_bytes)
    except Exception as e:
        logger.debug(f"[LaughSample] 擷取失敗（忽略）: {e}")


def laugh_counts(sink, voice_clients, now: float) -> tuple[int, int]:
    """回 (同時發聲人數, 在場非 bot 人數)。sink/voice_clients 缺 → 0。"""
    last_spoken = dict(getattr(sink, "user_last_spoken_time", {}) or {}) if sink else {}
    vocalizers = count_concurrent_voices(last_spoken, now=now, window=LAUGH_CONCURRENCY_WINDOW)
    present = 0
    if voice_clients and getattr(voice_clients[0], "channel", None):
        present = sum(1 for m in voice_clients[0].channel.members if not m.bot)
    return vocalizers, present


def snapshot_laugh_event(bot, store, speaker, timestamp, guild_id, channel_id) -> None:
    """拍快照並非阻塞寫 laugh_events（失敗吞掉）。"""
    try:
        vocalizers, present = laugh_counts(
            getattr(bot.engine, "sink", None), bot.voice_clients, time.time())
        asyncio.create_task(asyncio.to_thread(
            store.save_laugh_event,
            speaker, guild_id, channel_id, timestamp, vocalizers, present))
    except Exception as e:
        logger.debug(f"[LaughSnapshot] 快照失敗（忽略）: {e}")
