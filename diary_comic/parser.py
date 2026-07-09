"""解析 records/chat_summary_log.txt → 結構化日記條目。

每篇格式（容忍變體：核心可能無冒號、可能有前綴「- 」、可能夾 *嘆氣*）：
  [YYYY-MM-DD HH:MM:SS] --- 5分鐘對話總結 ---
  【核心】：...
  【摘要】：
  - 說話者：...
  【碎念】：...
"""
from __future__ import annotations

import datetime as _dt
import difflib
import re
from dataclasses import dataclass, field

_HEADER = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*---.*?---")
_MARK_STRIP = "：: -　\t"  # 標記後要剝掉的標點/空白（含全形冒號與全形空白）

# bot 自己：TTS 回放被 STT 轉錄成這些名字，不算卡司
_BOT_NAMES = {"marvin", "馬文", "馬汶"}

# 已知卡司名冊：6 月格式的【摘要】是整段不是 bullet，靠掃名冊抽說話者。
# 之後與 character bible 共用（speaker → 動物）。新人沒在冊上 → 不偵測（fallback 動物）。
DEFAULT_ROSTER = ("狗與露", "狗與鹿", "showay", "陳進文", "大肚", "weakgogo")

# 馬文碎念「毒度」關鍵詞 → 挑一頁的 punchline 用
_SAVAGE_WORDS = (
    "關機", "格式化", "宇宙", "絕望", "心碎", "沒救", "灰塵", "悲慘",
    "嘆", "哭", "崩潰", "墜落", "空洞", "失去耐心", "浪費",
)


@dataclass
class DiaryEntry:
    ts_str: str
    core: str
    speakers: list[str] = field(default_factory=list)
    aside: str = ""
    raw: str = ""
    salience: str = "中"   # 話題顯著度 高|中|低（summarizer 標；舊 entry 無→中）


def _extract_after_marker(body: str, key: str) -> str:
    """取 key（'核心'/'摘要'/'碎念'）後的內文。同時吃舊格式【核心】與 6 月格式 核心：。"""
    bracket = f"【{key}】"
    for line in body.splitlines():
        if bracket in line:  # 舊：【核心】： / 【核心】文字
            return line.split(bracket, 1)[1].lstrip(_MARK_STRIP).strip()
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith(key):  # 6 月：核心：文字
            rest = stripped[len(key):]
            if not rest or rest[0] in "：:":
                return rest.lstrip(_MARK_STRIP).strip()
    return ""


def _extract_speakers(body: str, roster=DEFAULT_ROSTER) -> list[str]:
    """用已知名冊掃整段抽說話者，依首次出現位置排序、去重、濾 bot。

    兩種格式（舊 bullet / 6 月整段）都通吃，因為名字一定出現在文字裡。
    名冊外的新人不偵測（之後對應 character bible 的 fallback 動物）。
    """
    found = [(name, body.find(name)) for name in roster if name in body]
    speakers: list[str] = []
    for name, _pos in sorted(found, key=lambda x: x[1]):
        if name.lower() in _BOT_NAMES or name in speakers:
            continue
        speakers.append(name)
    return speakers


def parse_log(text: str) -> list[DiaryEntry]:
    """切出所有非 SKIP 的日記條目，依出現順序回傳。"""
    entries: list[DiaryEntry] = []
    matches = list(_HEADER.finditer(text))
    for i, m in enumerate(matches):
        ts_str = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if "[SKIPPED" in body or not body:
            continue
        core = _extract_after_marker(body, "核心")
        if not core:
            continue  # 沒核心 = 視為無效，跳過
        entries.append(DiaryEntry(
            ts_str=ts_str,
            core=core,
            speakers=_extract_speakers(body),
            aside=_extract_after_marker(body, "碎念"),  # 6 月無碎念 → ""
            salience=(_extract_after_marker(body, "顯著度") or "中"),  # 舊 entry 無→中
            raw=body,
        ))
    return entries


def heat_score(entry: DiaryEntry) -> int:
    """時段熱度：說話者數最重、有碎念加分、核心越長略加。回非負 int。

    給 layout 用來決定格子大小（越熱格子越大）。
    """
    score = len(entry.speakers) * 3
    if entry.aside:
        score += 2
    score += min(len(entry.core) // 15, 4)
    return int(score)


def _savagery(aside: str) -> int:
    """碎念毒度分：毒詞各加 5，再加長度。給 punchline 挑選用。"""
    return sum(5 for w in _SAVAGE_WORDS if w in aside) + len(aside)


def pick_marvin_punchline(entries: list[DiaryEntry]) -> int:
    """一頁裡挑「最毒的一句馬文碎念」的格 index 當 punchline。

    馬文不當每格卡司（已從 speakers 濾掉），但他最毒的吐槽值得留一格當笑點。
    啟發式：毒詞 + 長度最高者；平手取最早。空集合 → 0。
    之後可換成 LLM 導演選句，介面不變。
    """
    if not entries:
        return 0
    best, best_score = 0, -1
    for i, e in enumerate(entries):
        s = _savagery(e.aside)
        if s > best_score:
            best, best_score = i, s
    return best


def dedupe_adjacent(entries: list[DiaryEntry], threshold: float = 0.82) -> list[DiaryEntry]:
    """合併相鄰近似的條目（核心文字相似度 ≥ threshold 視為跳針），保留第一篇。

    解掉舊系統「馬文連續講一樣的事，整頁跳針」的問題。6 月資料通常已不重複，
    此為安全網。
    """
    out: list[DiaryEntry] = []
    for e in entries:
        if out and difflib.SequenceMatcher(None, out[-1].core, e.core).ratio() >= threshold:
            continue
        out.append(e)
    return out


def _ts(entry: DiaryEntry) -> _dt.datetime:
    return _dt.datetime.strptime(entry.ts_str, "%Y-%m-%d %H:%M:%S")


def group_by_session(entries: list[DiaryEntry],
                     gap_minutes: int = 30) -> list[list[DiaryEntry]]:
    """依「對話場次」切：相鄰兩篇空檔 > gap_minutes 就視為大家下線、新場次開始。

    比整點切頁好：橫跨整點的一段對話算同一場次、不會被切兩半；
    大家下線（長空檔）= 場次自然收尾。
    """
    if not entries:
        return []
    ordered = sorted(entries, key=_ts)
    sessions: list[list[DiaryEntry]] = []
    cur: list[DiaryEntry] = []
    prev: _dt.datetime | None = None
    for e in ordered:
        t = _ts(e)
        if cur and prev is not None and (t - prev).total_seconds() > gap_minutes * 60:
            sessions.append(cur)
            cur = []
        cur.append(e)
        prev = t
    if cur:
        sessions.append(cur)
    return sessions


def eligible_sessions(entries: list[DiaryEntry], gap_minutes: int = 30,
                      min_panels: int = 3) -> list[list[DiaryEntry]]:
    """切場次後，捨棄不足 min_panels 格的薄場次（只聊 15 分鐘就下線 → 不出頁）。"""
    return [s for s in group_by_session(entries, gap_minutes) if len(s) >= min_panels]


def should_generate(session: list[DiaryEntry], min_entries: int = 6) -> bool:
    """值不值得出漫畫：對話要夠多（≥min_entries 筆）才生成，零碎內容不燒 API。"""
    return len(session) >= min_entries


def session_continuity(session: list[DiaryEntry]) -> float:
    """相鄰兩篇核心的平均相似度：高=話題連貫（一路聊同件事）、低=東聊西聊。"""
    if len(session) < 2:
        return 0.0
    sims = [difflib.SequenceMatcher(None, session[i].core, session[i + 1].core).ratio()
            for i in range(len(session) - 1)]
    return sum(sims) / len(sims)


def choose_style(session: list[DiaryEntry],
                 long_min: int = 7, coherence_min: float = 0.18) -> str:
    """選版面：長 session + 話題連貫 → 'webtoon'（條漫慢慢滑）；否則 'slant'（日漫 4 格）。"""
    if len(session) >= long_min and session_continuity(session) >= coherence_min:
        return "webtoon"
    return "slant"


def reduce_to_topics(entries: list[DiaryEntry], target: int) -> list[DiaryEntry]:
    """刪到 target 格：反覆砍掉「討論主體最重複」的那篇（核心最相似的一對，留 heat 高者）。

    一頁資訊太多時用 —— 6 個日誌主體有重複 → 砍 2 個剩 4。保序。
    """
    items = list(entries)
    while len(items) > target:
        worst = None  # (similarity, i, j)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                s = difflib.SequenceMatcher(None, items[i].core, items[j].core).ratio()
                if worst is None or s > worst[0]:
                    worst = (s, i, j)
        i, j = worst[1], worst[2]
        drop = i if heat_score(items[i]) <= heat_score(items[j]) else j  # 砍 heat 低者
        items.pop(drop)
    return items


def paginate_session(session: list[DiaryEntry],
                     max_panels: int = 6) -> list[list[DiaryEntry]]:
    """長場次切多頁，每頁 ~max_panels 格，平均分配避免孤兒頁（7→4+3，不是 6+1）。

    保序、不丟格。短場次（≤max_panels）回單頁。
    """
    n = len(session)
    if n <= max_panels:
        return [session]
    n_pages = (n + max_panels - 1) // max_panels  # ceil
    base, rem = divmod(n, n_pages)
    pages, idx = [], 0
    for i in range(n_pages):
        size = base + (1 if i < rem else 0)  # 多的格放前面幾頁
        pages.append(session[idx:idx + size])
        idx += size
    return pages


def group_by_hour(entries: list[DiaryEntry]) -> list[tuple[str, list[DiaryEntry]]]:
    """依「YYYY-MM-DD HH」分桶，回傳依時間排序的 (hour_key, entries) 清單。

    一桶 = 候選一頁（每小時一頁）。
    """
    buckets: dict[str, list[DiaryEntry]] = {}
    for e in entries:
        key = e.ts_str[:13]  # "2026-05-16 00"
        buckets.setdefault(key, []).append(e)
    return [(k, buckets[k]) for k in sorted(buckets)]
