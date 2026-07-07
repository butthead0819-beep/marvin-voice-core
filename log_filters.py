"""Log 衛生 filter。

CryptoErrorSampler：discord.ext.voice_recv.reader 的「CryptoError decoding packet data」
是 ERROR 級、每天 ~3800 行（有人講話時每個解不開的語音封包一行）。它不是系統性故障
（KeySync + Sentinel 已自癒真 desync，STT 在 800/min 爆量下仍存活），但 ERROR 級量大會
淹沒真正的錯誤、增加診斷難度。抽樣 1/N 放行（附累計數，仍看得到量），其餘 drop。
"""
from __future__ import annotations

import logging


class CryptoErrorSampler(logging.Filter):
    """抽樣 CryptoError ERROR：每 N 筆放行 1 筆（附累計數），其餘 drop。

    非 CryptoError 記錄一律放行、不計數（不影響其他 reader 警告/錯誤）。
    """

    _MATCH = "CryptoError"

    def __init__(self, sample_rate: int = 100):
        super().__init__()
        self._n = max(1, sample_rate)
        self._count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True   # 格式化失敗 → 不干預，照常放行
        if self._MATCH not in msg:
            return True
        self._count += 1
        if self._count % self._n == 1:
            record.msg = f"{msg}（CryptoError 抽樣 1/{self._n}，累計 {self._count}）"
            record.args = ()
            return True
        return False
