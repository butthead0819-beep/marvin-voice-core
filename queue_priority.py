"""使用者自選曲在 stream_queue 的插入位置（自 music_cog._user_song_insert_index 搬出）。"""
from __future__ import annotations


def is_marvin(item) -> bool:
    return str((item or {}).get('requested_by') or '').startswith('Marvin')


def user_song_insert_index(queue: list[dict], stream_mode: bool) -> int:
    """使用者自選曲的插入位置：排在所有既有使用者曲之後、第一首 Marvin 自動曲之前。

    爆音修（2026-08-27）：autopilot 播放中，點歌不插隊到「下一首」。歌尾時
    queue[0] 常已被 DJ tail 點火預載進 _preload_music_cache；使用者曲插它前面
    → 換源 preload cache miss → 歌尾邊界冷解碼（yt-dlp + ffmpeg + loudnorm/BPM）
    撞 DJ crossfade，CPU/executor 爆量 → mixer underrun → 大量爆音。往後推一首：
    已排定的那首先播、點歌自然變第三首，並多拿一整首歌的時間在背景把自己預載好。
    """
    if stream_mode and queue:
        # slot 0（下一首）神聖：歌尾常已被 DJ tail 預載，插它前面 → 爆音。
        # 從 slot 1 起找「使用者曲連續段」的尾巴，新點歌接在那之後。
        idx = 1
        while idx < len(queue) and not is_marvin(queue[idx]):
            idx += 1
        return idx
    # 非 autopilot：舊行為——插在第一首 Marvin 自動曲之前。
    for i, item in enumerate(queue):
        if is_marvin(item):
            return i
    return len(queue)
