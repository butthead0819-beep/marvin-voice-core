"""DJ 天氣提醒：在場的人各自城市的今日天氣（Open-Meteo，免費免 key）→ 扭蛋素材。
城市對照放 records/member_cities.json（gitignored：誰住哪是個資，不進 repo）。任何失敗回 []。"""
from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

MEMBER_CITIES_PATH = "records/member_cities.json"
CITY_COORDS: dict[str, tuple[float, float]] = {
    "台北": (25.0330, 121.5654), "新北": (25.0120, 121.4650), "桃園": (24.9937, 121.3010),
    "新竹": (24.8138, 120.9675), "台中": (24.1477, 120.6736), "台南": (22.9999, 120.2270),
    "高雄": (22.6273, 120.3014),
}
_CACHE_TTL_S = 30 * 60
_cache: dict[str, tuple[float, dict]] = {}


def load_member_cities(path: str = MEMBER_CITIES_PATH) -> dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def weather_tip(daily: dict) -> str:
    rain = daily.get("rain")
    if rain is not None and rain >= 50:
        return "記得帶傘"
    mx = daily.get("max")
    if mx is not None and mx >= 33:
        return "注意防曬多喝水"
    mn = daily.get("min")
    if mn is not None and mn <= 15:
        return "注意保暖"
    return ""


def weather_line(names: list[str], city: str, daily: dict) -> str:
    line = (
        f"{'、'.join(names)}在{city}："
        f"今天{round(daily['min'])}到{round(daily['max'])}度，降雨機率{daily['rain']}%"
    )
    tip = weather_tip(daily)
    if tip:
        line += f"（提醒：{tip}）"
    return line


async def fetch_daily(city: str) -> dict | None:
    if city not in CITY_COORDS:
        return None
    cached = _cache.get(city)
    if cached and time.time() - cached[0] < _CACHE_TTL_S:
        return cached[1]
    try:
        import aiohttp
    except Exception:
        return None
    try:
        lat, lon = CITY_COORDS[city]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "Asia/Taipei",
                    "forecast_days": 1,
                },
            ) as resp:
                j = await resp.json()
        daily = j["daily"]
        rain = daily["precipitation_probability_max"][0]
        result = {
            "max": float(daily["temperature_2m_max"][0]),
            "min": float(daily["temperature_2m_min"][0]),
            "rain": int(rain) if rain is not None else 0,
        }
        _cache[city] = (time.time(), result)
        return result
    except Exception as e:
        logger.debug(f"[dj_weather] fetch_daily({city}) 失敗: {e}")
        return None


async def weather_items(present: set[str] | None) -> list[str]:
    if os.environ.get("MARVIN_DJ_WEATHER", "1") == "0":
        return []
    try:
        if not present:
            return []
        cities = load_member_cities()
        by_city: dict[str, list[str]] = {}
        for name in present:
            city = cities.get(name)
            if city:
                by_city.setdefault(city, []).append(name)
        lines = []
        for city in sorted(by_city):
            names = sorted(by_city[city])
            daily = await fetch_daily(city)
            if daily:
                lines.append(weather_line(names, city, daily))
        lines.sort(key=lambda line: 0 if "提醒：" in line else 1)
        return lines
    except Exception:
        return []
