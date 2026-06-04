"""每首歌響度正規化（2026-06-04）。

Plan 12 音樂路徑原本不做任何 loudnorm（動態 loudnorm 會 pumping/悶，使用者實測拿掉
「好多了」），但歌與歌之間響度差大 → 使用者一直手動調音量。解法：背景取樣歌曲
25/50/75% 三點量整合響度 → 算到目標 LUFS 的「常數」增益，每首套一次（不在歌內持續
調 → 不 pumping）。

純函式（gain 計算 / 取樣位置 / ebur128 解析）放這檔，方便單測；實際 ffmpeg 量測 +
套用在 voice_controller。
"""
from __future__ import annotations

import re

TARGET_LUFS = -14.0       # 對齊既有 loudnorm I=-14
MAX_GAIN = 4.0            # 安靜歌最多放大 4x（+12dB），防過度放大噪音
MIN_GAIN = 0.25          # 大聲歌最多衰減到 0.25x（-12dB），防完全靜音


def compute_loudness_gain(measured_lufs: float | None,
                          *, target_lufs: float = TARGET_LUFS,
                          max_gain: float = MAX_GAIN, min_gain: float = MIN_GAIN) -> float:
    """整合響度（LUFS）→ 線性增益，使響度趨近 target。clamp 防爆音/過度放大。

    measured_lufs=None（量測失敗）→ 1.0（不調，graceful）。
    gain = 10^((target - measured)/20)：measured 比 target 安靜 → gain>1 放大；反之衰減。
    """
    if measured_lufs is None:
        return 1.0
    gain = 10 ** ((target_lufs - measured_lufs) / 20.0)
    return max(min_gain, min(max_gain, gain))


def sample_positions(duration_s: float, *, window_s: float = 20.0,
                     fracs: tuple[float, ...] = (0.25, 0.50, 0.75)) -> list[float]:
    """回各取樣起點秒數（25/50/75%）。歌太短（< 2*window）→ 退化成單點 0（從頭量）。

    起點 clamp 在 [0, duration-window]，避免 seek 過尾巴量到靜音。
    """
    if duration_s <= 0:
        return [0.0]
    if duration_s < window_s * 2:
        return [0.0]
    last_start = max(0.0, duration_s - window_s)
    out: list[float] = []
    for f in fracs:
        out.append(min(last_start, max(0.0, duration_s * f)))
    return out


def parse_ebur128_integrated(stderr: str) -> float | None:
    """從 ffmpeg ebur128 的 stderr summary 抽整合響度 'I: -XX.X LUFS'。抽不到回 None。"""
    if not stderr:
        return None
    # ebur128 Summary 段：'  I:         -14.5 LUFS'（最後一筆 Summary 才是整段整合值）
    matches = re.findall(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS", stderr)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def average_lufs(values: list[float | None]) -> float | None:
    """多點整合響度平均（過濾 None）。全 None → None。

    註：LUFS 是對數值，嚴格應做能量平均；但三點取樣只為估常數增益，算術平均誤差可接受
    （目標是把歌間差異從 ±十幾 dB 壓到幾 dB，不追求精準 EBU 合規）。
    """
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)
