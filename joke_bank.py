"""馬文冷笑話庫載入 + 「下一首歌名」拼音模糊比對。

設計（見 personas/joke_bank.yaml 檔頭、memory music_pinyin_fastpath）：
- 靠歌名語意/典故的笑話不做（要模型真的懂那首歌）；這裡只在「歌名字音」層碰撞：
  hook 拼音 ≈ 下一首歌名拼音 → 命中就播那則笑話。
- 比對：toneless 拼音上，hook 拼音必須是歌名拼音的「連續子字串」（音節邊界對齊）。
  拼音本身就是模糊層（吃掉同音字）；再疊 rapidfuzz 會讓「稻草 dao cao」誤中「曹操
  cao cao」這種——實測 partial_ratio 在短 hook 上假命中率爆高，故不用。
- hook 一律 ≥2 個中文字（單字太 promiscuous，"ai" 幾乎命中所有華語歌名）。
- 優雅降級：rapidfuzz / pypinyin / PyYAML 缺、或庫空 / 檔案壞 → match() 一律回 None
  （feature 自動關閉，不 crash bot）。

runtime 用法（cogs/music_cog.py::_fetch_dj_interjection_raw 笑話分支）：
    jk = get_joke_bank().match(next_song_title, exclude=recent_joke_texts)
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

DEFAULT_BANK = Path(__file__).parent / "personas" / "joke_bank.yaml"

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
    def __init__(self, path: Path | str = DEFAULT_BANK):
        self._path = Path(path)
        self._mtime = -1.0
        self._entries: list[dict] = []  # {joke, hook_pys: [str]}
        self._enabled = _DEPS_OK
        if _DEPS_OK:
            self._reload_if_stale()

    def _reload_if_stale(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._entries = []
            return
        if mtime == self._mtime:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or []
        except Exception as e:  # 檔案壞 → 保留舊的（或空），不 crash
            logger.warning(f"⚠️ [JokeBank] 載入失敗，feature 停用: {e}")
            return
        entries: list[dict] = []
        for row in raw:
            joke = (row.get("joke") or "").strip()
            hooks = [h.strip() for h in (row.get("hooks") or []) if h and len(_HAN.findall(h)) >= 2]
            if not joke or not hooks:
                continue
            entries.append({"joke": joke, "hook_pys": [to_pinyin(h) for h in hooks]})
        self._entries = entries
        self._mtime = mtime
        logger.info(f"😑 [JokeBank] 載入 {len(entries)} 則冷笑話")

    def match(self, song_title: str, *, exclude: set[str] | frozenset[str] = frozenset()) -> str | None:
        """下一首歌名 → 命中的笑話全文；沒命中回 None。exclude：近期播過的笑話全文，跳過。

        命中條件：某個 hook 的拼音，是歌名拼音裡「整段音節」的連續子串
        （前後都是空白或字串邊界 → 不會發生 "dao cao"⊂"cao caoxxx" 這種跨音節誤中）。
        多則命中 → 取 hook 音節數最長的（最specific）。
        """
        if not self._enabled or not song_title:
            return None
        self._reload_if_stale()
        if not self._entries:
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
