"""icy_meta.py — 車機顯示曲名/歌手/專輯/剩餘時間，第一刀：Mac 端把 now-playing 資訊
用標準 ICY in-band metadata 格式夾進 /audio_stream 的 MP3 bytes 流裡。

純函式＋純類別，無 IO：build_now_playing_payload() 組 payload bytes，IcyInterleaver
負責把 payload 依 ICY 規格插進音訊 byte stream。真正的網路/HTTP wiring 在
car_http_app.py::handle_audio_stream。
"""
from __future__ import annotations

import hashlib

ICY_METAINT = 16000          # 每 16000 bytes 音訊插一個 metadata 區塊（128kbps ≈ 1 秒）
ICY_FULL_REFRESH_S = 5.0     # 內容沒變時，至少每 5 秒重送一次完整 payload（重同步/位置校正）
TITLE_MAX_BYTES = 120
ARTIST_MAX_BYTES = 80
ALBUM_MAX_BYTES = 120
_SEP = "\x1f"


def truncate_utf8(s: str, max_bytes: int) -> str:
    """UTF-8 編碼後超過 max_bytes 就在字元邊界截斷（不能切出半個字元）。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    # 逐步縮短直到能合法解碼——UTF-8 截斷點可能落在多 byte 字元中間。
    while max_bytes > 0:
        try:
            return b[:max_bytes].decode("utf-8")
        except UnicodeDecodeError:
            max_bytes -= 1
    return ""


def _clean(s) -> str:
    if not s:
        return ""
    s = str(s)
    for ch in ("\x1f", "\n", "\r", "\x00"):
        s = s.replace(ch, " ")
    return s.strip()


def build_now_playing_payload(info: dict | None, start_time: float | None, now: float) -> bytes:
    """組出一行 ICY payload：M1\x1ftrack_id\x1fdur_ms\x1fpos_ms\x1fstate\x1ftitle\x1fartist\x1falbum。

    info 為 None/空 → 無歌在播，state=S、其餘欄位空。
    """
    if not info:
        return (_SEP.join(["M1", "", "0", "0", "S", "", "", ""])).encode("utf-8")

    title_raw = info.get("title", "")
    artist_raw = info.get("artist") or info.get("uploader", "") or ""
    album_raw = info.get("album", "") or ""

    title = truncate_utf8(_clean(title_raw), TITLE_MAX_BYTES)
    artist = truncate_utf8(_clean(artist_raw), ARTIST_MAX_BYTES)
    album = truncate_utf8(_clean(album_raw), ALBUM_MAX_BYTES)

    try:
        dur_ms = int(round(float(info.get("duration")) * 1000))
    except (TypeError, ValueError):
        dur_ms = 0

    if start_time is None:
        pos_ms = 0
    else:
        pos_ms = int((now - start_time) * 1000)
        pos_ms = max(0, pos_ms)
        if dur_ms:
            pos_ms = min(pos_ms, dur_ms)

    state = "P"

    track_id = hashlib.sha1(f"{title_raw}|{start_time}".encode("utf-8")).hexdigest()[:16]
    if track_id in ("ffffffffffffffff", "0000000000000000"):
        track_id = "0000000000000001"

    fields = ["M1", track_id, str(dur_ms), str(pos_ms), state, title, artist, album]
    return _SEP.join(fields).encode("utf-8")


class IcyInterleaver:
    """把 payload 依 ICY 規格插進連續的音訊 byte stream（跨 chunk 累計 metaint）。"""

    def __init__(self, metaint: int = ICY_METAINT, full_refresh_s: float = ICY_FULL_REFRESH_S):
        self._metaint = metaint
        self._full_refresh_s = full_refresh_s
        self._until_meta = metaint
        self._last_full_payload: bytes | None = None
        self._last_full_sent_at: float | None = None

    def _strip_pos(self, payload: bytes) -> bytes:
        # pos_ms 是第 4 個欄位（index 3），比對「內容是否變了」要忽略它，否則單純時間
        # 流逝就會被誤判成內容變化，每個 metaint 都送完整 payload。
        parts = payload.split(_SEP.encode("utf-8"))
        if len(parts) != 8:
            return payload
        parts[3] = b""
        return _SEP.encode("utf-8").join(parts)

    def _want_full(self, payload: bytes, now: float) -> bool:
        if self._last_full_payload is None:
            return True
        if self._strip_pos(payload) != self._strip_pos(self._last_full_payload):
            return True
        if self._last_full_sent_at is None or (now - self._last_full_sent_at) >= self._full_refresh_s:
            return True
        return False

    def _build_block(self, payload: bytes, now: float) -> bytes:
        if self._want_full(payload, now):
            body = payload
            if len(body) > 255 * 16:
                body = body[: 255 * 16]
            length = -(-len(body) // 16)  # ceil(len/16)
            padded = body + b"\x00" * (length * 16 - len(body))
            self._last_full_payload = payload
            self._last_full_sent_at = now
            return bytes([length]) + padded
        return b"\x00"

    def feed(self, chunk: bytes, payload: bytes, now: float) -> bytes:
        out = bytearray()
        offset = 0
        n = len(chunk)
        while offset < n:
            take = min(self._until_meta, n - offset)
            out += chunk[offset:offset + take]
            offset += take
            self._until_meta -= take
            if self._until_meta == 0:
                out += self._build_block(payload, now)
                self._until_meta = self._metaint
        return bytes(out)
