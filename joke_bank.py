"""馬文冷笑話庫載入 + 「下一首歌」比對。兩層：

1. **逐首歌專屬笑話**（personas/song_jokes.yaml，key = YouTube videoId）：autopilot 點到
   這首歌時，crossfade 剛好講這則專屬諧音/荒謬笑話（驚喜感）。由 scripts/
   generate_song_jokes.py 批次生 draft，Jack 逐則篩後搬進 yaml。
2. **泛用拼音 hook 庫**（personas/joke_bank.yaml）：第 1 層沒命中時的 fallback。歌名字音
   撞到某則笑話的 hook（toneless 拼音「整段音節連續子串」）就播那則。
   ⚠️ 不疊 rapidfuzz——短 hook 假命中率爆高（「稻草 dao cao」誤中「曹操 cao cao」）。
   拼音本身就是模糊層（吃同音字），子串比對已足夠。hook 一律 ≥2 中文字。

優雅降級：pypinyin / PyYAML 缺、或兩個檔案都空/壞 → match() 回 None（feature 自動關）。

runtime 用法（cogs/music_cog.py::_fetch_dj_interjection_raw 笑話分支）：
    jk = get_joke_bank().match(song_label, video_id=vid, exclude=recent_joke_texts)
    if jk: text = jk
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import yaml
    from music_fastpath import to_pinyin  # 共用既有 toneless 拼音轉換
    _DEPS_OK = True
except Exception:  # pragma: no cover - dep 缺才走這
    _DEPS_OK = False

_DIR = Path(__file__).parent / "personas"
DEFAULT_BANK = _DIR / "joke_bank.yaml"
DEFAULT_SONG_JOKES = _DIR / "song_jokes.yaml"

_HAN = re.compile(r"[一-鿿]")


def _clean_title(title: str) -> str:
    """歌名去雜訊：丟掉 YouTube 常見尾綴 / 括號註記 / 「歌手 - 」前綴，只留主標題字音。"""
    t = title or ""
    if " - " in t:
        t = t.split(" - ", 1)[1]
    t = re.sub(r"[\(\（\[【].*?[\)\）\]】]", " ", t)
    t = re.sub(r"(?i)\b(official|mv|music video|lyric|audio|hd|4k|live|feat\.?).*$", " ", t)
    return t.strip()


class JokeBank:
    def __init__(self, bank_path: Path | str = DEFAULT_BANK,
                 song_jokes_path: Path | str = DEFAULT_SONG_JOKES):
        self._bank_path = Path(bank_path)
        self._song_path = Path(song_jokes_path)
        self._bank_mtime = -1.0
        self._song_mtime = -1.0
        self._entries: list[dict] = []       # {joke, hook_pys: [str]}
        self._by_vid: dict[str, str] = {}    # videoId → joke
        self._enabled = _DEPS_OK
        if _DEPS_OK:
            self._reload_bank()
            self._reload_song_jokes()

    def _load_yaml(self, path: Path, mtime_attr: str):
        """回 (rows|None, new_mtime)。None = 沒變 or 讀不到（caller 保留舊資料）。"""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return [], -1.0  # 檔案不在 → 清空
        if mtime == getattr(self, mtime_attr):
            return None, mtime
        try:
            with open(path, "r", encoding="utf-8") as f:
                return (yaml.safe_load(f) or []), mtime
        except Exception as e:
            logger.warning(f"⚠️ [JokeBank] {path.name} 載入失敗，保留舊資料: {e}")
            return None, getattr(self, mtime_attr)

    def _reload_bank(self) -> None:
        rows, mtime = self._load_yaml(self._bank_path, "_bank_mtime")
        if rows is None:
            return
        entries: list[dict] = []
        for row in rows:
            joke = (row.get("joke") or "").strip()
            hooks = [h.strip() for h in (row.get("hooks") or []) if h and len(_HAN.findall(h)) >= 2]
            if joke and hooks:
                entries.append({"joke": joke, "hook_pys": [to_pinyin(h) for h in hooks]})
        self._entries, self._bank_mtime = entries, mtime
        logger.info(f"😑 [JokeBank] 泛用 hook 庫 {len(entries)} 則")

    def _reload_song_jokes(self) -> None:
        rows, mtime = self._load_yaml(self._song_path, "_song_mtime")
        if rows is None:
            return
        by_vid: dict[str, str] = {}
        for row in rows:
            key = (row.get("key") or "").strip()
            joke = (row.get("joke") or "").strip()
            if key and joke and (row.get("style") or "") != "skip":
                by_vid[key] = joke
        self._by_vid, self._song_mtime = by_vid, mtime
        logger.info(f"🎯 [JokeBank] 逐首歌專屬笑話 {len(by_vid)} 則")

    def match(self, song_title: str, *, video_id: str | None = None,
              exclude: set[str] | frozenset[str] = frozenset()) -> str | None:
        """下一首歌 → 命中的笑話全文；沒命中回 None。

        第 1 層：video_id 有專屬笑話 → 用它。
        第 2 層：歌名拼音撞到 hook 庫（某 hook 拼音是歌名拼音的整段音節連續子串；
                多則命中取 hook 音節數最長的）。
        exclude：近期播過的笑話全文，跳過。
        """
        if not self._enabled:
            return None
        self._reload_bank()
        self._reload_song_jokes()

        if video_id:
            jk = self._by_vid.get(video_id)
            if jk and jk not in exclude:
                return jk

        if not song_title:
            return None
        qpy = f" {to_pinyin(_clean_title(song_title))} "
        if not qpy.strip():
            return None
        best, best_len = None, 0
        for e in self._entries:
            if e["joke"] in exclude:
                continue
            for hp in e["hook_pys"]:
                if hp and f" {hp} " in qpy:
                    n = hp.count(" ") + 1  # 音節數
                    if n > best_len:
                        best_len, best = n, e
        return best["joke"] if best is not None else None


_SINGLETON: JokeBank | None = None


def get_joke_bank() -> JokeBank:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = JokeBank()
    return _SINGLETON
