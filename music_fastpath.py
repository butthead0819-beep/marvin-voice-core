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
import re
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

# fast-path 命中後回給 caller 的 query 要補回點歌動詞，否則裸「藝人 歌名」沒動詞 →
# IntentBus music agent 所有 play pattern 都不 match → bid 0.00 → bus drop → 不播 /
# Marvin LLM 假承諾「已為你播放」（2026-06-23 18:33 incident）。「放一首」命中 strong_play
# (0.95, 無 missing slot → 直接播不追問)，且播放時被 _extract_music_search_query 剝掉、
# 不污染 YT 搜尋（_extract cmd_prefixes 含「一首」）。
FASTPATH_PLAY_PREFIX = "放一首"


def to_play_command(canonical: str) -> str:
    """把 fast-path 命中的 canonical「藝人 歌名」包成 music agent 認得的點歌指令。"""
    return f"{FASTPATH_PLAY_PREFIX}{canonical}"


# 點歌命令前綴：真實 query 是「播放陶喆的流沙」，命令動詞要先剝掉只留歌名，
# 否則「播放/bo fang」當內容 token 被 token_set + 覆蓋率守門當噪音 → 命中失敗。
_CMD_PREFIX = re.compile(
    r"^(幫我|麻煩|我想|我要|可以|請)*"
    r"(播放|撥放|點播|播一首|放一首|來一首|來首|播|放|點|來|聽|想聽)+"
)


def strip_command_prefix(query: str) -> str:
    """剝點歌命令前綴（播放/放/點播…）只留歌名。剝完空則回原字串（避免整句被吃掉）。"""
    stripped = _CMD_PREFIX.sub("", query).strip()
    return stripped or query


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
        self._path = Path(catalog_path)
        self._names: list[str] = []
        self._index: dict[int, str] = {}  # idx → pinyin（rapidfuzz process choices）
        self._mtime: float = -1.0
        self._enabled = _DEPS_OK
        if _DEPS_OK:
            self._load()

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._index)

    def _load(self) -> None:
        """（重）載入目錄。reset 後重填，記錄 mtime 供熱重載比對。"""
        self._names = []
        self._index = {}
        try:
            self._mtime = self._path.stat().st_mtime
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            logger.info("[MusicFastPath] 目錄不存在/壞 → fast-path 停用 (%s)", self._path)
            return
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            py = row.get("pinyin") or to_pinyin(name)
            self._names.append(name)
            self._index[len(self._names) - 1] = py

    def _maybe_reload(self) -> None:
        """目錄檔 mtime 變了就熱重載（3am cron 重建後不用重啟 bot 就吃到新目錄）。"""
        try:
            mt = self._path.stat().st_mtime
        except OSError:
            return
        if mt != self._mtime:
            logger.info("[MusicFastPath] 目錄更新 → 熱重載 (%s)", self._path)
            self._load()

    def match(self, query: str) -> tuple[str, float] | None:
        """query（已剝喚醒詞）→ (canonical_name, score) 若 ≥門檻，否則 None。"""
        if not self._enabled or not query or not query.strip():
            return None
        self._maybe_reload()
        if not self._index:
            return None
        stripped = strip_command_prefix(query)
        qpy = to_pinyin(stripped)
        if not qpy:
            return None
        res = process.extractOne(qpy, self._index, scorer=fuzz.token_set_ratio)
        if res is None:
            return None
        _pinyin_val, score, idx = res
        if score >= self.threshold and self._title_covered(qpy, _pinyin_val, query_text=stripped):
            return self._names[idx], float(score)
        return None

    _STOP = {"de", "a", "ya", "ne", "ba", "la"}

    @classmethod
    def _title_covered(cls, query_py: str, cand_py: str, bar: float = 0.85,
                       query_text: str = "", min_song_tokens: int = 2) -> bool:
        """防「藝人對、歌錯」：兩道守門。

        ① 退化守門（2026-06-23）：用第一個「的」切出歌名（藝人在前），要求歌名內容 token
        （去 stopword）≥min_song_tokens。否則退化 query（如「對啊對啊」去掉「啊」只剩單一
        token dui）→ 藝人名又灌滿覆蓋率 → 配到同藝人別首假命中。token 太少 → 不敢 fast-path、
        回 None 走 cleaner。藝人 token 仍保留在②覆蓋率（同藝人是有效訊號，撐回近義糊字）。

        ② 覆蓋率：query 拼音 token（含藝人，去 stopword）大部分出現在命中曲名——擋只有
        藝人對、歌名沒對上的虛構/不在庫歌名。
        """
        if query_text:
            song_text = query_text.split("的", 1)[1] if "的" in query_text else query_text
            song_q = set(to_pinyin(song_text).split()) - cls._STOP
            if len(song_q) < min_song_tokens:
                return False
        q = set(query_py.split()) - cls._STOP
        if not q:
            return True
        c = set(cand_py.split())
        return len(q & c) / len(q) >= bar
