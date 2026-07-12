"""stt_debug_*.wav 的保留輪替。

這些是只寫不讀的音訊稽核落盤（discord_voice_engine 每次 STT 都封存一顆），
沒有任何程式回讀，唯一用途是人工事後聽診。無界累積會吃爆磁碟，故按檔名
內嵌的時間戳保留最近 N 天。

用檔名時間戳而非 mtime：shutil.copy 會把 mtime 設成複製當下，不代表擷取時間。
"""
import re
from datetime import datetime, timedelta
from pathlib import Path

# stt_debug_20260712_195715_20.2s.wav → 擷取時間 20260712_195715
_NAME_RE = re.compile(r"^stt_debug_(\d{8})_(\d{6})_.*\.wav$")


def prune_stt_debug(records_dir, now: datetime, retention_days: int = 7) -> list[Path]:
    """刪除 records_dir 下超過 retention_days 的 stt_debug_*.wav，回傳被刪清單。

    解析不出時間戳的檔案保守保留；last_stt_debug.wav 與非 stt_debug 檔不受影響。
    """
    cutoff = now - timedelta(days=retention_days)
    removed: list[Path] = []
    for path in Path(records_dir).glob("stt_debug_*.wav"):
        m = _NAME_RE.match(path.name)
        if not m:
            continue
        try:
            captured = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        if captured < cutoff:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
    return removed
