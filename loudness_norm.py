"""每首歌響度正規化（2026-06-04）。

Plan 12 音樂路徑原本不做任何 loudnorm（動態 loudnorm 會 pumping/悶，使用者實測拿掉
「好多了」），但歌與歌之間響度差大 → 使用者一直手動調音量。解法：背景取樣歌曲
25/50/75% 三點量整合響度（有起播點時改從起播點開始取樣）→ 算到目標 LUFS 的「常數」
增益，每首套一次（不在歌內持續調 → 不 pumping）。

純函式（gain 計算 / 取樣位置 / ebur128 解析）放這檔，方便單測；實際 ffmpeg 量測 +
套用在 voice_controller。
"""
from __future__ import annotations

import math
import re

TARGET_LUFS = -14.0       # 對齊既有 loudnorm I=-14
MAX_GAIN = 4.0            # 安靜歌最多放大 4x（+12dB），防過度放大噪音
MIN_GAIN = 0.25          # 大聲歌最多衰減到 0.25x（-12dB），防完全靜音
# 每點取樣秒數。20s→8s（2026-08-25）：三點×20s＝60s，對 180s 的歌等於解碼 1/3，
# 音樂 cog 端量測用的 ffmpeg -t 也要跟這裡同步（見 music_cog._measure_norm_gain_bg）。
DEFAULT_WINDOW_S = 8.0
DEFAULT_FRACS = (0.25, 0.50, 0.75)   # 從頭播：沿用舊取樣點
HIGHLIGHT_FRACS = (0.0, 1 / 3, 2 / 3)  # 有起播點（熱力圖精華/前奏跳過）：第一點就是起播點本身，
                                        # 以「實際第一耳聽到的位置」為準，其餘兩點平均分佈在剩下的播放區段


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


def sample_positions(duration_s: float, *, window_s: float = DEFAULT_WINDOW_S,
                     fracs: tuple[float, ...] | None = None,
                     start_s: float = 0.0) -> list[float]:
    """回各取樣起點秒數。支援 start_s（熱力圖精華起點對齊）。

    fracs 未指定時依 start_s 決定：start_s=0 用 DEFAULT_FRACS（25/50/75%，沿用舊行為）；
    start_s>0 用 HIGHLIGHT_FRACS（0/33/67%），第一個取樣點就是起播點本身。原因：舊版
    無條件用 25/50/75% 三個百分比取樣，當有 start_s（熱力圖精華起點）時完全沒量到
    start_s 本身這個「使用者第一耳聽到的位置」，取樣落在高潮之後較安靜的橋段/尾奏，
    導致增益被高估、開播時聲音過大（2026-09-24 修正）。明傳 fracs 時一律照傳入值，
    不受 start_s 影響。

    歌太短（有效長度 < 2*window）→ 退化成單點 start_s（從精華或開頭量）。
    起點 clamp 在 [start_s, duration-window]，避免 seek 過尾巴量到靜音。
    """
    start = max(0.0, float(start_s or 0.0))
    if fracs is None:
        fracs = HIGHLIGHT_FRACS if start > 0 else DEFAULT_FRACS
    if duration_s <= start:
        return [start]
    eff_duration = duration_s - start
    if eff_duration < window_s * 2:
        return [start]
    last_start = max(start, duration_s - window_s)
    out: list[float] = []
    for f in fracs:
        pos = start + eff_duration * f
        out.append(min(last_start, max(start, pos)))
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
    """多點整合響度能量平均（過濾 None）。全 None → None。

    LUFS 是對數值，能量平均（先轉線性能量再平均、轉回 dB）讓大聲段主導平均值，避免
    安靜段（前奏/尾奏）把平均拉低 → 增益被高估 → 開播爆音（2026-09-24 修正，原本用
    算術平均）。
    """
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return 10 * math.log10(sum(10 ** (v / 10) for v in nums) / len(nums))
