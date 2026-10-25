"""puck_skin.py — car puck 頭像底圖存放＋目前底圖指標。純 I/O 小模組，無 Discord 依賴。

Mac 端隨時可以 put() 一張新底圖（240×240 RGB888），之後 /audio_stream 的 ICY
payload（見 car_http_app.py）會把目前底圖的 etag 夾進第 12 欄，puck 偵測到變了就
去 GET /car_skin 下載換上（見 firmware/car_puck/car_puck.ino 的 skinFetch()）。

current()（ICY 高頻呼叫，~每 0.25s 一次）結果快取 1 秒，避免每格都碰磁碟；
read_current()（/car_skin 下載用）不快取，永遠讀最新。

沒有手動指定底圖時，依 SEASONAL 行事曆（本地日期）挑一張季節底圖。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path

SKIN_BYTES = 240 * 240 * 3
DEFAULT_DIR = Path.home() / "Library/Application Support/Marvin/puck_skins"

_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,40}$")
_CACHE_TTL_S = 1.0

# 季節服裝行事曆：(起 "MM-DD", 迄 "MM-DD"（含）, skin_id)。沒有手動指定底圖時才生效；
# 底圖檔 <skin_id>.rgb 要先用 puck_skin.py set 或手動放進 root。起 > 迄＝跨年。
SEASONAL: list[tuple[str, str, str]] = [
    ("10-01", "10-31", "halloween_pumpkin"),
]


class SkinStore:
    def __init__(self, root: Path = DEFAULT_DIR, *, clock=time.monotonic, today=None, seasonal=None):
        self._root = Path(root)
        self._clock = clock
        self._today = today if today is not None else datetime.date.today
        self._seasonal = seasonal if seasonal is not None else SEASONAL
        self._cache: tuple[str, str] | None = None
        self._cache_at: float | None = None

    @property
    def _current_path(self) -> Path:
        return self._root / "current.json"

    def _skin_path(self, skin_id: str) -> Path:
        return self._root / f"{skin_id}.rgb"

    def put(self, skin_id: str, data: bytes) -> str:
        if len(data) != SKIN_BYTES:
            raise ValueError(f"data must be exactly {SKIN_BYTES} bytes, got {len(data)}")
        if not _ID_RE.match(skin_id):
            raise ValueError(f"invalid skin_id: {skin_id!r}")

        self._root.mkdir(parents=True, exist_ok=True)
        etag = hashlib.sha1(data).hexdigest()[:8]

        self._skin_path(skin_id).write_bytes(data)

        tmp_path = self._current_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps({"id": skin_id, "etag": etag}), encoding="utf-8")
        os.replace(tmp_path, self._current_path)

        self._cache = None
        self._cache_at = None
        return etag

    def clear(self) -> None:
        try:
            self._current_path.unlink()
        except FileNotFoundError:
            pass
        self._cache = None
        self._cache_at = None

    def current(self) -> tuple[str, str] | None:
        now = self._clock()
        if self._cache_at is not None and (now - self._cache_at) < _CACHE_TTL_S:
            return self._cache

        result = self._read_current_file()
        self._cache = result
        self._cache_at = now
        return result

    def _read_current_file(self) -> tuple[str, str] | None:
        try:
            raw = self._current_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            skin_id = data["id"]
            etag = data["etag"]
        except (OSError, ValueError, KeyError):
            return self._seasonal_current()
        if not self._skin_path(skin_id).exists():
            return self._seasonal_current()
        return (skin_id, etag)

    def _seasonal_current(self) -> tuple[str, str] | None:
        md = self._today().strftime("%m-%d")
        for start, end, skin_id in self._seasonal:
            if start <= end:
                hit = start <= md <= end
            else:
                hit = md >= start or md <= end
            if not hit:
                continue
            try:
                data = self._skin_path(skin_id).read_bytes()
            except OSError:
                return None
            if len(data) != SKIN_BYTES:
                return None
            return (skin_id, hashlib.sha1(data).hexdigest()[:8])
        return None

    def read_current(self) -> tuple[str, bytes] | None:
        current = self._read_current_file()
        if current is None:
            return None
        skin_id, etag = current
        try:
            data = self._skin_path(skin_id).read_bytes()
        except OSError:
            return None
        if len(data) != SKIN_BYTES:
            return None
        return (etag, data)


if __name__ == "__main__":
    import sys

    def _main():
        if len(sys.argv) < 2:
            print("usage: puck_skin.py set <file.rgb> [--id ID] | clear | show")
            sys.exit(1)

        store = SkinStore()
        cmd = sys.argv[1]

        if cmd == "set":
            if len(sys.argv) < 3:
                print("usage: puck_skin.py set <file.rgb> [--id ID]")
                sys.exit(1)
            file_path = Path(sys.argv[2])
            skin_id = file_path.stem
            if "--id" in sys.argv:
                skin_id = sys.argv[sys.argv.index("--id") + 1]
            data = file_path.read_bytes()
            etag = store.put(skin_id, data)
            print(etag)
        elif cmd == "clear":
            store.clear()
        elif cmd == "show":
            print(store.current())
        else:
            print(f"unknown command: {cmd}")
            sys.exit(1)

    _main()
