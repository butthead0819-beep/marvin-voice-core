"""點歌 fast-path 匹配器：糊字 query → 拼音 fuzzy 比對乾淨 canonical 歌表。

命中（≥門檻）→ 回正規歌名，caller 可跳過 2.5s cleaner LLM 直接送 YT 播。
未命中 → 回 None，caller fall through 走正常 cleaner 路徑。

設計（見 memory music_pinyin_fastpath）：
- 比對目標是**正規化「歌手 歌名」**（ytmusicapi 結構化），不是 YT 髒標題。
- 中文 STT 錯誤多是同音字 → 在**拼音**上比對（pypinyin toneless），字元 fuzzy 救不回。
- scorer = rapidfuzz token_set_ratio；驗證顯示乾淨目錄上門檻 80 命中/拒絕乾淨分離。

優雅降級：rapidfuzz / pypinyin 缺、或目錄空 → match() 一律回 None（feature 自動關閉，
不 crash bot）。pypinyin 是新 dep（rapidfuzz 既有）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz, process
    from pypinyin import lazy_pinyin
    _DEPS_OK = True
except ImportError:  # 缺 dep → fast-path 靜默停用
    _DEPS_OK = False

DEFAULT_CATALOG = Path("records/music_catalog.json")
DEFAULT_THRESHOLD = 80.0


def to_pinyin(text: str) -> str:
    """中文 → toneless 拼音字串（英文/數字原樣 lower）。同音字常連聲調都被 STT 換掉，故去調。"""
    if not _DEPS_OK or not text:
        return ""
    return " ".join(lazy_pinyin(text)).lower()


class MusicFastPath:
    """載入 canonical 歌表、提供 query → (canonical, score) 匹配。

    catalog jsonl/json 格式：[{"name": "周杰倫 七里香", "pinyin": "..."}]
    pinyin 缺則 lazy 補算。
    """

    def __init__(self, catalog_path: Path | str = DEFAULT_CATALOG,
                 threshold: float = DEFAULT_THRESHOLD):
        self.threshold = threshold
        self._names: list[str] = []
        self._index: dict[int, str] = {}  # idx → pinyin（rapidfuzz process choices）
        self._enabled = _DEPS_OK
        if _DEPS_OK:
            self._load(Path(catalog_path))

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._index)

    def _load(self, path: Path) -> None:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            logger.info("[MusicFastPath] 目錄不存在/壞 → fast-path 停用 (%s)", path)
            return
        for i, row in enumerate(rows):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            py = row.get("pinyin") or to_pinyin(name)
            self._names.append(name)
            self._index[len(self._names) - 1] = py

    def match(self, query: str) -> tuple[str, float] | None:
        """query（已剝喚醒詞）→ (canonical_name, score) 若 ≥門檻，否則 None。"""
        if not self.enabled or not query or not query.strip():
            return None
        qpy = to_pinyin(query)
        if not qpy:
            return None
        res = process.extractOne(qpy, self._index, scorer=fuzz.token_set_ratio)
        if res is None:
            return None
        _pinyin_val, score, idx = res
        if score >= self.threshold and self._title_covered(qpy, _pinyin_val):
            return self._names[idx], float(score)
        return None

    @staticmethod
    def _title_covered(query_py: str, cand_py: str, bar: float = 0.85) -> bool:
        """防「藝人對、歌錯」：要求 query 拼音 token（去 stopword）大部分出現在命中曲名。

        token_set_ratio 會被共享的藝人名 token 灌分——query「王力宏的唯一」即使唯一
        不在庫，也因藝人名撐到 ≥門檻配到同藝人別首。要求 query 內容 token 覆蓋率達標，
        擋掉只有藝人對、歌名沒對上的情況（虛構/不在庫歌名 → fall through 走 cleaner）。
        """
        stop = {"de", "a", "ya", "ne", "ba", "la"}
        q = set(query_py.split()) - stop
        if not q:
            return True
        c = set(cand_py.split())
        return len(q & c) / len(q) >= bar
