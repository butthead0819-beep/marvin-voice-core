"""location_state.py — 車載 ESP32 puck 最新 GPS 訊號存取。"""
from __future__ import annotations

import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "location_state.json")


def load_location_state(path: str = DEFAULT_PATH) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_location_state(*, lat: float, lon: float, ts: float, path: str = DEFAULT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"lat": lat, "lon": lon, "ts": ts}, f)
