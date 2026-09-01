"""RescueOutcomeLogger / RescueWavStore — IntentBus rescue 觀察層的落地實作。

RescueOutcomeLogger：rescue_outcome_sink 的 JSONL 實作。無 dedup（每筆對 daily
ritual 都有分析價值）；無 rotation（records/ 目錄日累積，由 daily ritual 讀完後
archive，與 judge_outcomes 同款）。

RescueWavStore：audio-rescue 的原始 wav sidecar。wav bytes 不塞進 jsonl（base64
會把 rescue_outcomes.jsonl 撐爆、daily ritual 讀取失控），改寫成
records/rescue_wav/{tag}.wav，jsonl record 只記相對路徑。self-prune 到最新
`keep` 個檔，避免無 rotation 塞爆磁碟。replay harness（scripts/replay_audio_rescue.py）
吃這個目錄當語料。

Caller 注意：write 在 sync 路徑跑，但 disk IO 在 voice_controller 的場景
（每次 rescue 一筆 = 數秒一次）下沒有 latency 顧慮，不需 background task。
"""
from __future__ import annotations

import json
import re
from pathlib import Path


class RescueOutcomeLogger:
    def __init__(self, jsonl_path: Path | str):
        self.jsonl_path = Path(jsonl_path)

    def write(self, record: dict) -> None:
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# tag 只用來當檔名主幹（.wav 副檔名另外加），不需要點號——連點號一起擋掉
# 順便消除「..」路徑穿越的疑慮。
_TAG_SAFE = re.compile(r"[^0-9A-Za-z_-]+")


class RescueWavStore:
    """audio-rescue 原始 wav 的 sidecar store。write() 回傳相對路徑供 jsonl 記錄。"""

    def __init__(self, wav_dir: Path | str, *, keep: int = 500):
        self.wav_dir = Path(wav_dir)
        self.keep = max(1, keep)

    def write(self, wav_bytes: bytes, tag: str) -> str | None:
        """寫 {tag}.wav，回傳相對於 CWD 的路徑字串。空 bytes → None。"""
        if not wav_bytes:
            return None
        safe = _TAG_SAFE.sub("_", tag).strip("_") or "utt"
        self.wav_dir.mkdir(parents=True, exist_ok=True)
        path = self.wav_dir / f"{safe}.wav"
        path.write_bytes(bytes(wav_bytes))
        self._prune()
        try:
            return str(path.relative_to(Path.cwd()))
        except ValueError:
            return str(path)

    def _prune(self) -> None:
        wavs = sorted(self.wav_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        for stale in wavs[:-self.keep]:
            try:
                stale.unlink()
            except OSError:
                pass
