"""LLM 品味 profile + 鄰近歌手 seed（autopilot T2 的「離線 biased expert」，2026-06-04）。

動機：T2 radio / get_played_seed_ids 只能找「跟已播歌相似」的歌——困在群組點播史的
回音室。LLM 讀 liked/played 歌 → 推出**史外鄰近歌手**（伍佰/Beyond…），再用 ytmusic
search 解析成真 videoId（resolve-then-trust 防幻覺），餵進 T2 seed 池 → 能擴到群組
沒聽過的鄰近歌手，又不飄太遠（PoC 實測 weakgogo→伍佰 radio 回落到他愛的茄子蛋）。

設計（對齊 [[triadic_expert_pattern_domain_and_timing]]）：
  - biased expert（LLM 文化跳躍）跑**離線/每日**，昂貴+有雜訊的學習丟離線。
  - runtime（T2）只讀快取 videoId → radio 落地，語音熱路徑不打 LLM/search。
  - 所有 LLM 呼叫走 bus（[[feedback_llm_calls_must_use_bus]]）：daily batch → call_paid_review。

純函式（build/parse）+ 可注入 IO（call_fn / client）→ 全可單測無網路。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import memory_sandbox
from music_recommender import Evidence

logger = logging.getLogger(__name__)

TASTE_SYSTEM_PROMPT = (
    "你是華語音樂品味分析師。根據使用者實際聽的歌，輸出 JSON："
    '{"profile":"2-3句品味描述(歌手/年代/曲風/情緒)",'
    '"adjacent_artists":["5個他沒聽但鄰近、會喜歡的歌手"],'
    '"suggested_songs":[{"artist":"歌手","title":"歌名"}],'
    '"avoid_artists":["3-5個依他品味推斷會明顯反感、絕對不要推的歌手或曲風代表"]}'
    "（建議6首真實存在的歌；adjacent_artists 跳出他已聽的歌手但風格相鄰；"
    "avoid_artists 只列你高度確定他會排斥的，寧缺勿濫）。只輸出 JSON。"
)

_EMPTY = {"profile": "", "adjacent_artists": [], "suggested_songs": [], "avoid_artists": []}


def build_taste_input(songs: list[str], likes: list[str], skipped: list[str] | None = None) -> str:
    """組 LLM user prompt（純函式）。

    skipped：這位使用者按過跳過的歌名（負向訊號）。給了才附加——沒有這段時
    LLM 只看「聽過什麼」推 avoid_artists 等於瞎猜；有明確被跳過的歌可以讓
    avoid_artists 有憑有據，而不是純粹反向聯想聽過的曲風。
    """
    lines = "\n".join(f"- {t}" for t in songs if t)
    out = f"使用者聽過的歌：\n{lines}\n他的興趣標籤：{likes}"
    if skipped:
        skip_lines = "\n".join(f"- {t}" for t in skipped)
        out += f"\n他跳過不聽的歌（負向訊號，可用來判斷 avoid_artists）：\n{skip_lines}"
    return out


def parse_taste_response(raw: str) -> dict:
    """解析 LLM 輸出 → {profile, adjacent_artists, suggested_songs}。

    壞 JSON / 缺欄位 → graceful 預設（不丟例外，讓 caller 安全略過）。
    """
    if not raw:
        return dict(_EMPTY)
    s = raw.strip()
    # 容錯：抓第一個 {...} 區塊
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return dict(_EMPTY)
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return dict(_EMPTY)
    if not isinstance(data, dict):
        return dict(_EMPTY)
    aa = data.get("adjacent_artists") or []
    ss = data.get("suggested_songs") or []
    av = data.get("avoid_artists") or []
    return {
        "profile": str(data.get("profile", "") or ""),
        "adjacent_artists": [str(a) for a in aa if a] if isinstance(aa, list) else [],
        "suggested_songs": ss if isinstance(ss, list) else [],
        "avoid_artists": [str(a) for a in av if a] if isinstance(av, list) else [],
    }


async def generate_taste_profile(songs: list[str], likes: list[str], *, call_fn,
                                   skipped: list[str] | None = None) -> dict | None:
    """LLM 生品味 profile。call_fn(content, system) -> str|None（注入 bus 呼叫）。

    沒歌 → 不打 LLM 回 None；LLM 全失敗（call_fn 回 None）→ None。
    """
    if not songs:
        return None
    raw = await call_fn(build_taste_input(songs, likes, skipped), TASTE_SYSTEM_PROMPT)
    if not raw:
        return None
    return parse_taste_response(raw)


async def resolve_artist_seeds(artists: list[str], *, client, per_artist: int = 1) -> list[str]:
    """鄰近歌手名 → ytmusic search → 真 videoId（resolve-then-trust 防幻覺）。

    去重、保序；search 無果（幻覺歌手）跳過；任何 search 例外跳過該歌手。
    """
    out: list[str] = []
    seen: set[str] = set()
    for a in artists:
        if not a:
            continue
        try:
            res = client.search(a, filter="songs", limit=per_artist)
        except Exception:
            continue
        for r in (res or [])[:per_artist]:
            vid = r.get("videoId")
            if vid and vid not in seen:
                seen.add(vid)
                out.append(vid)
    return out


# ── 快取（runtime state，gitignored）─────────────────────────────────────────
def read_profiles(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_profile(path, user: str, data: dict) -> None:
    """寫單一使用者 profile（含 seed_video_ids + ts）。合併既有檔。"""
    if memory_sandbox.active():
        return  # 沙盒：整檔覆寫 no-op（ephemeral）
    p = Path(path)
    profiles = read_profiles(p)
    entry = dict(data)
    entry["ts"] = time.time()
    profiles[user] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")


def _fresh_field(path, users: list[str], max_age_s: float, field: str) -> list[str]:
    """在場成員某欄位的聯集（過 max_age 丟掉，缺/壞檔回 []）。去重保序。"""
    profiles = read_profiles(path)
    now = time.time()
    out: list[str] = []
    seen: set[str] = set()
    for u in users:
        e = profiles.get(u)
        if not isinstance(e, dict):
            continue
        if now - e.get("ts", 0) > max_age_s:
            continue
        for v in e.get(field, []) or []:
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def fresh_seed_ids(path, users: list[str], max_age_s: float) -> list[str]:
    """在場成員的快取鄰近 seed videoId（正向：餵 T2 radio seed）。"""
    return _fresh_field(path, users, max_age_s, "seed_video_ids")


def fresh_avoid_artists(path, users: list[str], max_age_s: float) -> list[str]:
    """在場成員的 avoid_artists 聯集（負空間：T2 radio 候選的額外排除）。"""
    return _fresh_field(path, users, max_age_s, "avoid_artists")


def fresh_adjacent_artists(path, users: list[str], max_age_s: float) -> list[str]:
    """在場成員的 adjacent_artists 聯集（正向：LLM 推的史外鄰近歌手，餵 T4 catalog search）。"""
    return _fresh_field(path, users, max_age_s, "adjacent_artists")


def extract_discover_new_evidence(artist: str, adjacent_artists: list[str]) -> Evidence | None:
    """從 adjacent_artists 抽新領域訊號 Evidence，供解釋層槽位填空使用。

    只有候選歌手命中 adjacent_artists 清單才回 Evidence（signal_type="adjacent_artist"，
    沒有 listen 事件所以 timestamp/play_count 都是空值）；沒命中回 None（非壞資料，
    只是候選本身不屬於新領域訊號，caller 該當作沒有 evidence 處理）。

    fail-open：adjacent_artists 型別異常（非 list）→ log 記錄、回傳 None，不拋例外。
    """
    try:
        if not isinstance(adjacent_artists, list):
            raise TypeError(f"adjacent_artists 應為 list，實際 {type(adjacent_artists)!r}")
    except TypeError as e:
        logger.warning("discover_new evidence 抽取失敗，跳過藝人 %r：%s", artist, e)
        return None
    if not artist or artist not in adjacent_artists:
        return None
    return Evidence(
        signal_type="adjacent_artist",
        timestamp=None,
        play_count=0,
        skip_count=0,
        source_tier="adjacent_artist",
        subject="you",
        requester=None,
    )


def sanitize_profile(profile: dict, core_artists: list[str]) -> dict:
    """用 deterministic 指紋（`taste_fingerprint.compute_taste_fingerprint` 的
    per_user core_artists）交叉驗證 LLM profile，濾掉自相矛盾/沒擴展價值的輸出。

    LLM 偶爾會把使用者明顯愛聽（真人點播統計出的核心藝人）的歌手塞進
    avoid_artists（自相矛盾，直接砍）或 adjacent_artists（不是真的「跳出史外」，
    只是換句話說同一個已經在聽的歌手，沒有擴展價值）。純函式、不改原物件；
    沒有 core_artists 可比對 → 原樣回傳（fail-open，不因指紋缺資料砍光 LLM 輸出）。
    """
    if not core_artists:
        return dict(profile)
    core = [c.strip() for c in core_artists if c and c.strip()]

    def _matches_core(name: str) -> bool:
        n = (name or "").strip()
        if not n:
            return False
        return any(c == n or c in n or n in c for c in core)

    out = dict(profile)
    avoid = profile.get("avoid_artists") or []
    adjacent = profile.get("adjacent_artists") or []
    dropped_avoid = [a for a in avoid if _matches_core(a)]
    dropped_adjacent = [a for a in adjacent if _matches_core(a)]
    out["avoid_artists"] = [a for a in avoid if a not in dropped_avoid]
    out["adjacent_artists"] = [a for a in adjacent if a not in dropped_adjacent]
    if dropped_avoid:
        logger.warning("taste profile 自相矛盾：avoid_artists 含 core_artists %r，已濾掉", dropped_avoid)
    if dropped_adjacent:
        logger.warning("taste profile adjacent_artists 含既有 core_artists %r（非史外），已濾掉", dropped_adjacent)
    return out


def filter_avoided(candidates: list[dict], avoid_artists: list[str]) -> list[dict]:
    """剔除 artist 命中 avoid 的候選（純函式）。normalize 比對：avoid 名出現在候選
    artist 字串內即剔（涵蓋「伍佰」vs「伍佰 & China Blue」）。avoid 空 → 原樣回。"""
    if not avoid_artists:
        return candidates
    av = [a.strip().lower() for a in avoid_artists if a and a.strip()]
    if not av:
        return candidates
    out = []
    for c in candidates:
        artist = (c.get("artist") or "").lower()
        if any(a in artist for a in av):
            continue
        out.append(c)
    return out
