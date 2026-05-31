"""hotswap_loudness — loudnorm 量測解析 + stream2 音量匹配 filter（Plan 11 Slice 2）。

stream1 用動態 loudnorm 播整首歌；hotswap 切到 stream2 後若只用固定 volume，
整首剩餘段落音量會跟原本（loudnorm 正規化過）不一致——切換瞬間被 ducking 遮掉，
但之後持續可聞。

解法：stream2 的音樂用 **linear loudnorm**（2-pass 量測值 → 常數增益、同 -14 LUFS
target、無暫態），匹配 stream1 的目標響度。量測失敗則 fallback 固定 volume
（= Slice 1 行為，不炸）。

純函式：subprocess 量測在 voice_controller，這裡只負責解析與 filter 構建（可測）。
"""
from __future__ import annotations

import json

# 與 play_stream_song 既有 loudnorm 參數一致（line 6992 / 7004）
LOUDNORM_TARGET = "I=-14:TP=-1.5:LRA=11"

# linear loudnorm 必須的量測欄位（缺一不可組）
_REQUIRED = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


def parse_loudnorm_measurement(ffmpeg_stderr: str) -> dict | None:
    """從 ffmpeg `loudnorm=...:print_format=json` 的 stderr 抽量測值。

    loudnorm 把 JSON 印在 stderr 最後一個 {} 區塊。回 None = 沒抓到合法量測
    （→ caller fallback 固定 volume）。
    """
    start = ffmpeg_stderr.rfind("{")
    end = ffmpeg_stderr.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(ffmpeg_stderr[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not all(k in data for k in _REQUIRED):
        return None
    return {k: data[k] for k in _REQUIRED}


def build_stream2_music_filter(measured: dict | None, vol: float) -> str:
    """stream2 的 `[1:a] → [music]` filter。

    有量測值 → linear loudnorm（常數增益、無暫態、匹配 stream1 -14 target）+ volume；
    無 → 固定 volume（Slice 1 fallback）。兩者都不加 afade（實聽證實 afade 放大爆音）。
    """
    if measured:
        return f"[1:a]{_linear_loudnorm(measured)},volume={vol:.3f}[music]"
    return f"[1:a]volume={vol:.3f}[music]"


def _linear_loudnorm(measured: dict) -> str:
    """linear loudnorm（常數增益、無暫態、匹配 stream1 -14 target）的 filter 字串。"""
    return (
        f"loudnorm={LOUDNORM_TARGET}:linear=true"
        f":measured_I={measured['input_i']}"
        f":measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}"
        f":measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}"
    )


def build_volume_swap_af(measured: dict | None, vol: float) -> str:
    """語音調音量即時生效用的單輸入 `-af` 字串（stream2 = 同首歌 -ss + 新音量，無 TTS）。

    無 ducking 遮接縫 → stream2 必須跟 stream1 同響度行為：有量測用 linear loudnorm 匹配，
    無量測 fallback **保留** dynamic loudnorm（對齊 play_stream_song line 7081），不像 TTS
    路徑直接裸 volume（那條靠 ducking 蓋掉瞬間響度差，這條沒有）。
    """
    ln = _linear_loudnorm(measured) if measured else f"loudnorm={LOUDNORM_TARGET}"
    return f"{ln},volume={vol:.3f}"
