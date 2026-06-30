"""generate_dynamic_system_msg 的降載快取（disk-backed，撐過頻繁重啟）。

LLM pool 可用量檢討發現 generate_dynamic_system_msg 佔全部 LLM 呼叫 35%。兩類降載：
- **純評語**（songs_request/release_*/cooldown… 固定 prompt、不吃 context）：一次批次生 N 句
  變體存池，runtime 隨機輪播 → 近乎零 LLM、零品質損失（Marvin 吐槽本就該有重複感）。
- **DJ 介紹**（radio/stream/dj_interjection 吃 context）：按 (event_type, context) 快取，
  同首歌重播重用（autopilot 重播很兇）→ 砍重複生成，不動「新歌獨特介紹」。

純函式 + disk JSON，clock/rng 可注入供測。fail-open：壞檔/IO 失敗 → 當空快取，不 crash。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time

DEFAULT_PATH = "records/dynamic_msg_cache.json"
QUIP_TTL_S = 7 * 86400      # 評語池每 7 天刷新（撈進 persona/toxicity 飄移）
DJ_TTL_S = 30 * 86400       # 同首歌 DJ 介紹 30 天內重用
QUIP_POOL_SIZE = 8

_NUM_PREFIX = re.compile(r"^\s*(?:\d+[\.\)、:：]|[-*•])\s*")
_QUOTES = "「」『』\"'“”‘’()（）"


def parse_quips(raw: str) -> list[str]:
    """把 LLM 批次輸出（每句一行，可能帶編號/引號）解析成乾淨評語清單。<2 句回 []（caller 退回單句）。"""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        s = _NUM_PREFIX.sub("", line).strip().strip(_QUOTES).strip()
        if len(s) < 2 or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out if len(out) >= 2 else []


class DynamicMsgCache:
    def __init__(self, path: str = DEFAULT_PATH, *, now=time.time, rng=None):
        self._path = path
        self._now = now
        self._rng = rng or random
        self._data = self._load()

    def _load(self) -> dict:
        try:
            d = json.load(open(self._path, encoding="utf-8"))
            return {"quips": d.get("quips", {}), "dj": d.get("dj", {})}
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return {"quips": {}, "dj": {}}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except OSError:
            pass  # fail-open：寫不進去不影響功能（下次再生）

    # ── 純評語池 ────────────────────────────────────────────────────────────
    def get_quip(self, event_type: str) -> str | None:
        e = self._data["quips"].get(event_type)
        if not e or not e.get("items"):
            return None
        if self._now() - e.get("ts", 0) > QUIP_TTL_S:
            return None
        return self._rng.choice(e["items"])

    def set_quips(self, event_type: str, items: list[str]) -> None:
        items = [s.strip() for s in (items or []) if s and s.strip()]
        if not items:
            return
        self._data["quips"][event_type] = {"items": items, "ts": self._now()}
        self._save()

    # ── DJ 介紹（按 context 快取）─────────────────────────────────────────────
    @staticmethod
    def _dj_key(event_type: str, context: str) -> str:
        h = hashlib.sha1((context or "").encode("utf-8")).hexdigest()[:16]
        return f"{event_type}:{h}"

    def get_dj(self, event_type: str, context: str) -> str | None:
        e = self._data["dj"].get(self._dj_key(event_type, context))
        if not e:
            return None
        if self._now() - e.get("ts", 0) > DJ_TTL_S:
            return None
        return e.get("text")

    def set_dj(self, event_type: str, context: str, text: str) -> None:
        if not text:
            return
        self._data["dj"][self._dj_key(event_type, context)] = {"text": text, "ts": self._now()}
        self._save()
