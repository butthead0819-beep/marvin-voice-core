"""Spotify Search field-scoped query 建構（純函式，無 I/O，供同步/非同步呼叫端共用）。

Why：Spotify `track:`/`artist:` 欄位限定搜尋比自由文字精準一個量級，但對「中英
雙語合併」字串很敏感（"track:我們很好 Better Days" 查不到，"track:我們很好" 才
秒配到；"artist:林俊傑 JJ Lin" 查不到，"artist:JJ Lin" 才配到）。中/英文各自拆開、
剝掉標題裡重複的藝人名 token 後再組合出候選 query。

實測踩過的雷（scripts/spotify_clean_music_memory.py 開發時發現）：
  • title/uploader 常「藝人 標題」字序不一致（"林俊傑 JJ Lin" vs "JJ Lin林俊傑"）、
    無分隔符黏在一起 → 逐 token 比對移除藝人名，別用「開頭字串完全比對」。
  • CJK 用 regex findall 天生按連續字元斷成語意塊（"零度的親吻"/"華納"/"高畫質"
    各自一塊），YouTube 標題慣例「標題在前、廠牌/畫質雜訊尾隨」→ 只取第一塊當
    主要候選最準；全部合併留當備援。英文則是逐字分詞，合併留著才拼得回完整標題。
  • `track:` 欄位對殘留雜訊極敏感——一有沒被 cruft 清單認出的雜訊字（如「華納」
    「高畫質」）整段就查不到，不是只降低排序。
"""
from __future__ import annotations

import re

from song_name_clean import _is_pure_cruft, _CRUFT_IN

_CJK = re.compile(r"[一-鿿぀-ヿ]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z'.]*")

MAX_FIELD_QUERIES = 3


def split_lang(s: str) -> tuple[list[str], list[str]]:
    """中英混合字串拆成 (CJK token list, 拉丁 token list)，保留斷詞邊界。"""
    return _CJK.findall(s or ""), _LATIN.findall(s or "")


def _title_track_candidates(cleaned_title: str, artist_full: str) -> tuple[list[str], str]:
    title_cjk_tokens, title_latin_tokens = split_lang(cleaned_title)
    artist_cjk_tokens, artist_latin_tokens = split_lang(artist_full)
    artist_cjk_set, artist_latin_set = set(artist_cjk_tokens), set(artist_latin_tokens)

    kept_cjk = [t for t in title_cjk_tokens if t not in artist_cjk_set and not _is_pure_cruft(t)]
    kept_latin = [
        t for t in title_latin_tokens
        if t not in artist_latin_set and not _CRUFT_IN.fullmatch(t)
    ]

    cjk_candidates = []
    if kept_cjk:
        cjk_candidates.append(kept_cjk[0])
        if len(kept_cjk) > 1:
            cjk_candidates.append(" ".join(kept_cjk))
    latin_candidate = " ".join(kept_latin).strip()
    return cjk_candidates, latin_candidate


def build_field_queries(cleaned_title: str, artist_full: str) -> list[tuple[str, str]]:
    """回 [(query, track_candidate), ...]（track_candidate 供呼叫端命中後比對相似度守門）。"""
    cjk_candidates, title_latin = _title_track_candidates(cleaned_title, artist_full)
    artist_cjk_tokens, artist_latin_tokens = split_lang(artist_full)
    artist_latin = " ".join(artist_latin_tokens)
    artist_cjk = " ".join(artist_cjk_tokens)

    track_candidates = [c for c in cjk_candidates + [title_latin] if c]
    artist_candidates = [c for c in [artist_latin, artist_cjk] if c]

    queries, seen = [], set()
    for t in track_candidates:
        for a in artist_candidates:
            q = f"track:{t} artist:{a}"
            if q not in seen:
                seen.add(q)
                queries.append((q, t))
        if not artist_candidates:
            q = f"track:{t}"
            if q not in seen:
                seen.add(q)
                queries.append((q, t))
    return queries[:MAX_FIELD_QUERIES]
