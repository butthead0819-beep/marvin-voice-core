"""gps_context.py — GPS 訊號 → DJ prompt 用的區級地點描述（純函式，無 I/O）。

只需要「哪一區」讓 DJ 有話講，不需要街道精度——不用外部 reverse-geocode 服務
（座標是敏感資料，別送第三方），用台北市 12 區中心點做最近點比對就夠。

訊號源只有 ESP32 車載 puck（隨身帶著，跟人一起移動，每 15 分鐘回報一次）；
DJ 播報只在車上或家裡播出，家裡直接用預設城市，不需要另一條手機定位訊號。
"""
from __future__ import annotations

DEFAULT_CITY = "台中"
CAR_MAX_AGE_S = 15 * 60

# 台北市 12 區粗略中心點（度）。只需要「哪一區」，不需要街道精度。
_DISTRICTS: dict[str, tuple[float, float]] = {
    "中正區": (25.0322, 121.5199),
    "大同區": (25.0630, 121.5133),
    "中山區": (25.0636, 121.5262),
    "松山區": (25.0499, 121.5578),
    "大安區": (25.0265, 121.5436),
    "萬華區": (25.0345, 121.4999),
    "信義區": (25.0308, 121.5645),
    "士林區": (25.0919, 121.5254),
    "北投區": (25.1325, 121.4989),
    "內湖區": (25.0693, 121.5885),
    "南港區": (25.0554, 121.6066),
    "文山區": (24.9887, 121.5701),
}


def nearest_district(lat: float, lon: float) -> str:
    """最近中心點的區名（平面近似，區級夠用不需要真大地距離）。"""
    return min(
        _DISTRICTS,
        key=lambda name: (_DISTRICTS[name][0] - lat) ** 2 + (_DISTRICTS[name][1] - lon) ** 2,
    )


def city_label(state: dict | None, now: float, default: str = DEFAULT_CITY) -> str:
    """location_state.json 讀出的最新車載 GPS 訊號 → DJ 環境行用的區名。

    訊號過期（超過 15 分鐘）或沒有訊號 → default。
    """
    if not state:
        return default
    ts = state.get("ts")
    lat, lon = state.get("lat"), state.get("lon")
    if ts is None or lat is None or lon is None:
        return default
    if now - ts > CAR_MAX_AGE_S:
        return default
    return nearest_district(lat, lon)
