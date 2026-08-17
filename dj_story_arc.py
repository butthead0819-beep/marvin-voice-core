"""DJ 故事弧線（Story Arc）串場 — 5 步驟管線：找敘事流 → 共同/個人回憶 → 大綱+選歌
（真候選池）→ 口白（拿到定案歌單才寫）→ resolve/記錄（不碰播放）。

跟 `themed_playlist.py`（單一批次「有主題的歌單」）的差異：這裡是一段跨 N 首歌的線性
敘事（開場→發展→高潮→收尾），口白是拿到已定案的歌單順序才寫（能做「承接上一首、
開啟下一首」的過渡，不只是單首前導白），每個節點還帶播放微調提示（bpm_target/
volume_delta_db）。

找敘事流：`chat_summary_log.txt` 每則是10分鐘對話總結，帶 `meme_id`（語義主題標籤）。
`_pick_narrative_day` 用「同日 meme_id 重複＝有發展的敘事線」當優先訊號，但這訊號覆蓋率
只有 ~16.5%、值域偏泛用類別詞——所以是軟性排序不是硬性門檻：優先選有 meme 主線的一天，
沒有就 fallback 選「共同回憶最多」的一天，兩者都撈不到才回 None。

多使用者共同敘事：素材依 LifeCore.speakers 跟 story members 的交集分兩桶——
交集 ≥2 人 → 「共同回憶」(shared_cores)，撐主線；交集 =1 人 → 那個人的「個人回憶」
(member_cores[member])，只給他自己的專屬節點用，絕不可講成好像是別人也經歷過的
（同 [[feedback_recommend_attribution_rule]] 掛名鐵則的邏輯：講錯比不講還傷）。
每位 story member 至少要有一個 spotlight_member 是自己的節點——這是靠 prompt
指示做「軟性強制」，parse 完不通過整條丟棄，靠 caller/preview 腳本檢查覆蓋率
（漏了誰，人類審查時看得到，不做自動重試）。

節點之間可以靠「共鳴」串接不同人的個人回憶（不需要兩人真的同場在場）——例如
A 的事件讓人聯想到 B 也有過類似經歷，用 `resonance_link` 標記被呼應到的那個人，
但口白仍要分清楚哪段是誰的事，不能把兩人的事混成一件講。所有提及具體使用者+
事件的地方語氣一律是溫暖共鳴，禁止針對、roast、嘲諷——即使素材聽起來像糗事，
也要往「這段經歷讓人有共鳴」寫，不能拿來調侃當事人。

選歌：候選池（`music_recommender.build_member_pools` 的 group_resonance/liked/
spotlight/long_tail lane）只當口味參考顯示給 LLM，**不是硬性限制**——LLM 可以自由
推薦候選池外的「驚喜」新歌，`tag_taste_match` 只是逐節點標記選中的歌有沒有命中候選池
（`taste_match`），純資訊性、不砍節點。存在性/可播性交給 `resolve_story_arc`
（resolve-then-VERIFY）把關——這才是真正擋掉「歌不存在」的地方。

整段弧線包裝成一個「節目」：`build_show_intro` 產生片頭——片頭音樂是固定的節目主題曲
（不隨集數變化，人工用 Suno/Gemini 生成後放在 `DEFAULT_INTRO_MUSIC_PATH`，這裡只給路徑
不合成音訊），片頭引導口白是純模板組字（不額外打 LLM）：只用已經生成/驗證過的
`arc_title`/`members`/`node_count`/`target_duration_s`，零捏造風險、零成本。

本模組只做 Phase 1（離線可驗、不碰播放）：
- gather_story_brief：純函式。narrative_day 當天生活素材（拆共同/個人）+ 口味/對話/liked
  → StoryBrief。
- build_story_candidate_pools：純函式，薄封裝 music_recommender.build_member_pools。
- build_outline_prompt / parse_story_outline：純函式（Call 1 prompt 組裝 / JSON 解析）。
- tag_taste_match：純函式，選歌是否命中候選池的資訊性標記（不砍節點）。
- curate_story_outline：協調器（Call 1），call_fn 注入 → 走 bus 付費池。
- build_interjection_prompt / parse_story_interjections：純函式（Call 2）。
- curate_story_interjections：協調器（Call 2），拿到定案歌單才寫口白。
- build_show_intro：純函式，模板組字片頭口白 + 固定片頭音樂路徑。
- resolve_story_arc：逐 node resolve(artist+song) → 品質閘（完全複用 themed_playlist 的
  三道閘：resolve-then-VERIFY、非單曲、vid 去重）→ enqueue-ready info dicts。
- record_story_arc：日記用 jsonl 落地。
- build_staged_show / save_staged_show / load_staged_show / clear_staged_show：
  Prepare（生成+TTS預渲染）跟 Play（純播放）兩階段拆分用——「待播節目」持久化到
  `records/dj_story_arc_staged.json`，播放當下零 LLM/零 TTS 延遲，可跨時間、跨 bot
  重啟排程觸發（實際播放協程 `MusicCog._play_story_arc` 在 `cogs/music_cog.py`）。

Phase 2（不在此檔）：中途插歌的「部分重規劃」（replan_story_arc）、bpm/volume
實際套用到播放、spotlight 覆蓋率不足時的自動重試。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

import dj_life_context
from themed_playlist import _norm_for_match  # 共用正規化比對，避免重複定義

_STORY_ARC_LOG = "records/dj_story_arcs.jsonl"
DEFAULT_INTRO_MUSIC_PATH = "assets/dj_sfx/show_intro.mp3"  # 節目片頭主題曲，人工放置
_BGM_POSITION_PATH = "records/dj_story_bgm_position.json"
ROUGH_TTS_S_PER_CHAR = 0.3          # 只供離線預覽粗估口白時長，非真實 TTS 校準值

DEFAULT_AVG_SONG_LENGTH_S = 240.0   # 4 分鐘，反推 node_count 用的粗估值
MIN_NODE_COUNT = 3
MAX_NODE_COUNT = 8
DEFAULT_MAX_SHARED_CORES = 12
DEFAULT_MAX_MEMBER_CORES = 4
DEFAULT_SHORTLIST_SIZE = 8          # 每個候選 lane 給 LLM 看的候選歌數量


@dataclass
class StoryBrief:
    narrative_day: str                 # 敘事流取材的那一天（YYYY-MM-DD），debug/record 用
    shared_cores: list[str]            # speakers 交集 story members ≥2 人的核心句 → 主線用
    member_cores: dict[str, list[str]] # 每位 member 的個人核心句 → 該 member 專屬節點用
    liked_items: list[str]             # suki.get_recent_liked_items 文字（已掛名）
    conv_snippets: list[str]           # conv_buffer 近期對話
    members: list[str]                 # 故事對象
    target_duration_s: float
    node_count: int                    # 至少 len(members)，夾在 [3, 8]（每人至少一個節點）


@dataclass
class StoryNode:
    position: int
    emotion_tag: str
    song_query: dict           # {"period":..., "people":[...], "motif":..., "free_text":...}
    artist: str
    song: str
    interjection_script: str
    bpm_target: float | None
    volume_delta_db: float
    spotlight_member: str | None = None  # 這節點是誰的專屬高光時刻；None=屬於共同主線
    resonance_link: str | None = None    # 這節點的敘事有沒有呼應到另一位成員的類似經歷
    taste_match: bool = True             # 選歌有沒有命中候選池（貼合已知口味）；純資訊性標記，
                                          # 不影響節點去留——False 代表 LLM 的「驚喜」推薦


@dataclass
class StoryArc:
    arc_title: str
    nodes: list[StoryNode]

    def spotlight_coverage(self, members: list[str]) -> list[str]:
        """回傳沒有任何專屬高光節點的 member 清單（空 list = 每人都被顧到）。

        供 caller/preview 腳本檢查用；不是自動重試，只是把「LLM 有沒有真的照 prompt
        指示做到每人至少一個 spotlight」攤開給人類審查看。
        """
        covered = {n.spotlight_member for n in self.nodes if n.spotlight_member}
        return [m for m in members if m not in covered]


@dataclass
class ShowIntro:
    intro_script: str        # 片頭引導口白（TTS）
    intro_music_path: str    # 片頭主題曲檔案路徑（人工放置，這裡不合成音訊）


def build_show_intro(arc: StoryArc, brief: StoryBrief, *,
                     intro_music_path: str = DEFAULT_INTRO_MUSIC_PATH) -> ShowIntro:
    """純函式。模板組字片頭口白——只用已生成/驗證過的 arc_title/members/node_count/
    target_duration_s，零額外 LLM call、零捏造風險（跟現有口白鐵則「不確定不要編」一致，
    模板天生不會編，比另開一次 LLM call 更安全）。

    片頭音樂是固定的節目主題曲，不隨集數變化——由人工用 Suno/Gemini 等工具生成後放在
    `intro_music_path`；本函式只回傳路徑，不檢查檔案是否存在（那是播放/預覽層的事）。
    """
    members_label = "、".join(brief.members) or "大家"
    minutes = max(1, round(brief.target_duration_s / 60.0))
    script = (
        f"歡迎回到讀空氣時間。今晚要說的故事是《{arc.arc_title}》，獻給{members_label}。"
        f"接下來的{minutes}分鐘，{len(arc.nodes)}首歌，我們開始吧。"
    )
    return ShowIntro(intro_script=script, intro_music_path=intro_music_path)


class BgmCursor:
    """記住口白 BGM 播到哪，讓每段口白接續播放不從頭開始（方案B：位置記憶重觸發，
    非常駐背景聲道——改動只在「記住/回傳位置」，不用動 mixer 的聲道架構）。

    每段口白觸發播放時 `peek()` 拿起始秒數（給 `ffmpeg -ss <offset>` 用），播完後
    `advance(played_s)` 記錄這段實際播了多久，下一段 `peek()` 就接著那個位置。

    跟 `dj_topic_selector.TopicCooldownStore` 同樣的 JSON + tmp-replace 原子寫模式
    （可變狀態要原子寫，跟 `record_story_arc` 的 append-only JSONL 是不同用途）。
    fail-open：讀寫失敗都不炸斷播放，退化成從頭播（`peek()` 回 0.0）。

    Phase 1 只提供這個可測的位置記憶工具，實際在 `_run_tail_dj`/`_play_dj_tail_sfx`
    播放時呼叫 `peek()`/`advance()` 是 Phase 2 播放整合的事，這裡不碰。
    """

    def __init__(self, path: str = _BGM_POSITION_PATH):
        self._path = path

    def peek(self) -> float:
        """回傳下一段該從第幾秒開始播；讀取失敗/檔案不存在 → 0.0（從頭播）。"""
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            return float(data.get("offset_s", 0.0))
        except Exception:
            return 0.0

    def advance(self, played_s: float, *, track_duration_s: float | None = None) -> float:
        """這段實際播了 played_s 秒 → 更新位置，供下一段 peek() 接續。

        track_duration_s 有給時，超過曲長會回捲到開頭（BGM 通常比整場節目短，
        播完一輪要繞回去而不是卡在結尾）。回傳新 offset（供呼叫端/測試檢視）。
        """
        new_offset = self.peek() + max(0.0, played_s)
        if track_duration_s and track_duration_s > 0:
            new_offset = new_offset % track_duration_s
        self._write(new_offset)
        return new_offset

    def reset(self) -> None:
        """新一場節目開始時歸零——同一支 BGM 檔案，新的 show 不該接續上一場的位置。"""
        self._write(0.0)

    def _write(self, offset_s: float) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"offset_s": offset_s}, f)
            os.replace(tmp, self._path)
        except Exception:
            pass


def estimate_interjection_duration_s(script: str, *,
                                     s_per_char: float = ROUGH_TTS_S_PER_CHAR) -> float:
    """純函式。純供離線預覽粗估口白 TTS 時長（非真實校準值，Phase 2 播放時要用實際
    TTS 產出的音檔長度取代這個估計）。"""
    return len((script or "").strip()) * s_per_char


# ── Step 1: 找敘事流 ──────────────────────────────────────────────────────────

def _count_shared(cores: list, members: list[str]) -> int:
    """cores 是 dj_life_context.LifeCore 列表；回傳 speakers 交集 members ≥2 的則數。"""
    member_set = set(members)
    return sum(1 for c in cores if len(member_set & set(c.speakers)) >= 2)


def _life_cores_by_day(summary_entries, members: list[str], *, now: float,
                       days: float, max_len: int = dj_life_context.DEFAULT_MAX_LEN
                       ) -> dict[str, list]:
    """近 days 天生活核心句依日期（YYYY-MM-DD）分組，不截斷（跟
    `dj_life_context.recent_life_cores_with_speakers` 的差異：那個函式最後會
    `[-max_cores:]` 裁到全域最新幾條，這裡要看每一天完整的量才能挑出敘事流最豐富的一天）。

    複用 `dj_life_context._fields`/`_is_privacy_safe`/`LifeCore` 的過濾邏輯
    （同一份 privacy filter，只是輸出改成按天分組、不裁切）。
    """
    present_speakers = set(members)
    cutoff = now - days * 86400.0
    by_day: dict[str, list] = defaultdict(list)
    for e in summary_entries:
        ts_str, core, salience = dj_life_context._fields(e)
        if not core or not str(core).strip():
            continue
        if str(salience).strip() == "低":
            continue
        try:
            ts = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        if not dj_life_context._is_privacy_safe(e, present_speakers):
            continue
        c = str(core).strip()[:max_len]
        label = f"【重點】{c}" if str(salience).strip() == "高" else c
        raw_meme = getattr(e, "meme_id", "")
        meme_id = raw_meme.strip() if isinstance(raw_meme, str) else ""
        raw_speakers = getattr(e, "speakers", None)
        speakers = tuple(raw_speakers) if isinstance(raw_speakers, (list, tuple)) else ()
        by_day[ts_str[:10]].append(dj_life_context.LifeCore(label, meme_id, speakers))
    return dict(by_day)


def _pick_narrative_day(entries_by_day: dict[str, list], members: list[str],
                        min_cores: int) -> str | None:
    """純函式。挑一天當敘事流主線：優先「同日 meme_id 重複 ≥2 次」的天，
    同分再看「共同回憶則數」，都沒有訊號時單看共同回憶則數。

    最高分那天共同回憶仍 < min_cores → 回 None（沒有任何一天材料夠撐主線）。
    """
    scored: list[tuple[int, int, str]] = []
    for day, cores in entries_by_day.items():
        meme_counts = Counter(c.meme_id for c in cores if c.meme_id)
        meme_repeat_bonus = 1 if any(v >= 2 for v in meme_counts.values()) else 0
        scored.append((meme_repeat_bonus, _count_shared(cores, members), day))
    if not scored:
        return None
    scored.sort(reverse=True)
    best_bonus, best_shared, best_day = scored[0]
    if best_shared < min_cores:
        return None
    return best_day


# ── Step 2: 共同/個人回憶（narrative_day 當天）────────────────────────────────

def gather_story_brief(summary_entries, members: list[str],
                       liked_items: list[str], conv_snippets: list[str], *,
                       now: float, target_duration_s: float,
                       avg_song_length_s: float = DEFAULT_AVG_SONG_LENGTH_S,
                       days: float = 7.0, min_cores: int = 2,
                       max_shared_cores: int = DEFAULT_MAX_SHARED_CORES,
                       max_member_cores: int = DEFAULT_MAX_MEMBER_CORES) -> StoryBrief | None:
    """純函式。先挑敘事流主線那一天（`_pick_narrative_day`），只用當天的核心句依
    speakers 交集拆「共同/個人」兩桶 + liked/對話素材 + 目標時長 → StoryBrief。

    沒有任何一天材料夠（共同回憶 < min_cores）→ 回 None（caller fallback，不中斷音樂）。
    """
    entries_by_day = _life_cores_by_day(summary_entries, members, now=now, days=days)
    narrative_day = _pick_narrative_day(entries_by_day, members, min_cores)
    if narrative_day is None:
        return None
    day_cores = entries_by_day[narrative_day]
    member_set = set(members)
    shared_cores: list[str] = []
    member_cores: dict[str, list[str]] = {m: [] for m in members}
    for c in day_cores:
        overlap = member_set & set(c.speakers)
        if len(overlap) >= 2:
            shared_cores.append(c.text)
        elif len(overlap) == 1:
            member_cores[next(iter(overlap))].append(c.text)
        # overlap 空（跟任何 story member 都無關）→ 丟掉，不進任何一桶
    shared_cores = shared_cores[-max_shared_cores:]
    member_cores = {m: v[-max_member_cores:] for m, v in member_cores.items()}
    node_count = max(MIN_NODE_COUNT, len(members),
                     min(MAX_NODE_COUNT, round(target_duration_s / avg_song_length_s)))
    return StoryBrief(narrative_day=narrative_day, shared_cores=shared_cores,
                      member_cores=member_cores, liked_items=list(liked_items),
                      conv_snippets=list(conv_snippets), members=list(members),
                      target_duration_s=target_duration_s, node_count=node_count)


# ── Step 3a: 真候選池（music_recommender 薄封裝）─────────────────────────────

def _shortlist(candidates: list, limit: int) -> list:
    """依分數排序取前 limit（決定性，不用 pick_candidate 的加權隨機抽樣——
    離線預覽/測試要可重現）。"""
    return sorted(candidates, key=lambda c: c.score, reverse=True)[:limit]


def build_story_candidate_pools(members: list[str], songs: dict, exclude_titles: list[str],
                                *, now: float,
                                shortlist_size: int = DEFAULT_SHORTLIST_SIZE,
                                include_long_tail: bool = True) -> dict[str, list]:
    """純函式。薄封裝 `music_recommender.build_member_pools`：
    `pools["shared"]` = 各 member 池的 group_resonance lane 合併去重（大家共同喜歡）；
    池是空的話（`connections` 欄位嚴格要求同場共鳴，實測常常是空的）**放寬** fallback 成
    「同一首歌同時出現在 ≥2 位 member 各自候選池裡」（不論哪個 lane）——沒有正式共鳴標記
    也算共同口味的軟訊號。
    `pools[member]` = 該 member 池的 liked/spotlight lane（個人口味）+ `include_long_tail`
    時再加 long_tail lane（點過但久沒播）——**放寬**個人候選數量，這條 lane 實測通常比
    liked/spotlight 大很多，是候選池太薄時最有效的補充來源。

    songs/exclude_titles/now 皆為參數注入（同 `build_member_pools` 本身就是純函式），
    呼叫端自己組 `mm.all_songs()` 傳進來，不在此函式內碰 MusicMemory。
    """
    from music_recommender import build_member_pools

    all_pools = build_member_pools(members=members, songs=songs,
                                   exclude_titles=exclude_titles, now=now)
    personal_lanes = ("liked", "spotlight", "long_tail") if include_long_tail else ("liked", "spotlight")

    result: dict[str, list] = {}
    for m in members:
        personal = [c for c in all_pools.get(m, []) if c.lane in personal_lanes]
        result[m] = _shortlist(personal, shortlist_size)

    shared_candidates = []
    seen = set()
    for m in members:
        for c in all_pools.get(m, []):
            if c.lane != "group_resonance":
                continue
            key = (_norm_for_match(c.anchor_artist), _norm_for_match(c.anchor_title))
            if key in seen:
                continue
            seen.add(key)
            shared_candidates.append(c)

    if not shared_candidates:
        # 放寬 fallback：同一首歌出現在 ≥2 位 member 的候選池（不限 lane）→ 當共同候選，
        # 每首歌保留分數最高的那個 Candidate 代表。
        title_owners: dict[tuple[str, str], set[str]] = defaultdict(set)
        title_best: dict[tuple[str, str], object] = {}
        for m in members:
            for c in all_pools.get(m, []):
                key = (_norm_for_match(c.anchor_artist), _norm_for_match(c.anchor_title))
                title_owners[key].add(m)
                if key not in title_best or c.score > title_best[key].score:
                    title_best[key] = c
        shared_candidates = [title_best[key] for key, owners in title_owners.items()
                             if len(owners) >= 2]

    result["shared"] = _shortlist(shared_candidates, shortlist_size)
    return result


# ── Step 3b/4: Call 1 — 大綱 + 選歌（限候選池內）─────────────────────────────

_OUTLINE_SYS = (
    "你是一位懂這群人的 DJ，要用 {node_count} 首歌的時間說完一個故事，故事對象是：{members}。\n"
    "規則：\n"
    "1) 故事是【線性】的：開場→發展→高潮→收尾，剛好 {node_count} 個節點，每個節點對應一首歌。\n"
    "2) 素材分兩種：【共同回憶】是大家都在場經歷的事，拿來撐主線劇情；【XX的個人回憶】只屬於"
    "標註的那個人。\n"
    "3) {members} 每個人都要有至少一個節點是他的專屬高光時刻：那個節點的 spotlight_member 欄位"
    "設成那個人的名字；其餘屬於主線共同故事的節點，spotlight_member 設 null。\n"
    "4) 節點之間可以靠【共鳴】串起不同人的個人回憶，不需要兩人真的同場經歷。有做這種呼應的"
    "節點，resonance_link 欄位填被呼應到的那個人的名字；沒有就填 null。\n"
    "5) 每個節點的 song_query 必須是【具體】的人物/時期/意象標籤組合（例如「小時候＋外婆＋"
    "糖果」），不准用空泛情緒詞。song_query 底下要有 period（時期）、people（人物列表）、"
    "motif（意象）、free_text（一句話補充）四個欄位，沒有的留空字串/空陣列即可。"
    "【song_query 也適用不捏造規則】period/people/motif/free_text 只能根據上面給的【共同"
    "回憶】【個人回憶】【最近喜歡過的東西】真實內容來寫，素材沒提到的細節（人物、事件、"
    "物品）絕對不要編——寧可寫得籠統、留空，也不要捏造出素材裡沒有的東西。\n"
    "6) 【選歌】下面給的候選歌清單是這群人的口味參考，不是唯一選項——你可以挑清單裡的，也"
    "可以自由推薦清單外真實存在的歌（鼓勵推薦驚喜、有新鮮感的發現），只要貼合這群人的口味"
    "方向、貼合這個節點的敘事就好。artist/song 必須是真實存在的歌（真歌手＋真歌名），不確定"
    "就不要編、寧可挑清單裡有把握的。\n"
    "7) bpm_target（60-180 的整數）跟 volume_delta_db（-6 到 +3）依故事節奏給——開場/收尾"
    "通常較慢較輕，高潮較快較滿。\n"
    '只回 JSON：{{"arc_title":"…","nodes":[{{"position":1,"emotion_tag":"…",'
    '"spotlight_member":null,"resonance_link":null,'
    '"song_query":{{"period":"…","people":["…"],"motif":"…","free_text":"…"}},'
    '"artist":"…","song":"…","bpm_target":90,"volume_delta_db":0}}, ...]}}'
)


def _format_pool(label: str, candidates: list) -> str:
    lines = "\n".join(f"  {i}. {c.anchor_artist} - {c.anchor_title}"
                      for i, c in enumerate(candidates, 1)) or "  （無候選）"
    return f"【{label}】\n{lines}"


def build_outline_prompt(brief: StoryBrief, candidate_pools: dict[str, list],
                         exclude_titles: list[str]) -> tuple[str, str]:
    """純函式 → (system, user)。Call 1：共同/個人核心句 + 候選歌 shortlist 組進 user。"""
    members_label = "、".join(brief.members) or "群聊"
    system = _OUTLINE_SYS.format(node_count=brief.node_count, members=members_label)
    shared_cores_text = "\n".join(f"- {c}" for c in brief.shared_cores) or "（無）"
    member_blocks = []
    for m, cs in brief.member_cores.items():
        lines = "\n".join(f"  - {c}" for c in cs) or "  （無）"
        member_blocks.append(f"【{m}的個人回憶】\n{lines}")
    member_text = "\n".join(member_blocks) or "（無）"
    liked = "、".join(brief.liked_items) or "（無）"
    conv = "\n".join(f"- {c}" for c in brief.conv_snippets) or "（無）"
    excl = "、".join(exclude_titles[:80]) or "（無）"
    pool_blocks = [_format_pool("共同候選", candidate_pools.get("shared", []))]
    for m in brief.members:
        pool_blocks.append(_format_pool(f"{m}的個人候選", candidate_pools.get(m, [])))
    pools_text = "\n".join(pool_blocks)
    user = (
        f"故事對象：{members_label}\n\n"
        f"【共同回憶】（大家都在場，可以放心當主線）：\n{shared_cores_text}\n\n"
        f"{member_text}\n\n"
        f"最近喜歡過的東西：{liked}\n\n"
        f"最近聊的內容：\n{conv}\n\n"
        f"候選歌（口味參考，不是唯一選項）：\n{pools_text}\n\n"
        f"目標總時長：約 {brief.target_duration_s:.0f} 秒（{brief.node_count} 首歌）\n"
        f"已經放過/不要再選的歌（歌名）：{excl}\n\n"
        f"請用 {brief.node_count} 首歌說完一個線性故事，回 JSON。"
    )
    return system, user


def parse_story_outline(resp: str, *, max_nodes: int = MAX_NODE_COUNT) -> StoryArc | None:
    """純函式。Call 1 的 LLM JSON → StoryArc（`interjection_script` 留空字串，Call 2 才填）；
    空/壞/無 title/無 nodes → None。

    node 缺 artist/song 的整條丟掉。依 position 排序，position 重複時保留先出現者。
    spotlight_member/resonance_link 空字串正規化成 None。
    """
    if not resp:
        return None
    m = re.search(r"\{.*\}", resp, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    title = str(d.get("arc_title", "")).strip()
    raw = d.get("nodes") if isinstance(d.get("nodes"), list) else []
    seen_positions: set[int] = set()
    nodes: list[StoryNode] = []
    for n in raw:
        if not isinstance(n, dict):
            continue
        artist = str(n.get("artist", "")).strip()
        song = str(n.get("song", "")).strip()
        if not (artist and song):
            continue
        try:
            position = int(n.get("position"))
        except (TypeError, ValueError):
            position = len(nodes) + 1
        if position in seen_positions:
            continue
        seen_positions.add(position)
        song_query = n.get("song_query") if isinstance(n.get("song_query"), dict) else {}
        try:
            bpm_target = float(n.get("bpm_target")) if n.get("bpm_target") is not None else None
        except (TypeError, ValueError):
            bpm_target = None
        try:
            volume_delta_db = float(n.get("volume_delta_db") or 0.0)
        except (TypeError, ValueError):
            volume_delta_db = 0.0
        spotlight_member = n.get("spotlight_member")
        spotlight_member = str(spotlight_member).strip() if spotlight_member else None
        resonance_link = n.get("resonance_link")
        resonance_link = str(resonance_link).strip() if resonance_link else None
        nodes.append(StoryNode(
            position=position, emotion_tag=str(n.get("emotion_tag", "")).strip(),
            song_query=song_query, artist=artist, song=song, interjection_script="",
            bpm_target=bpm_target, volume_delta_db=volume_delta_db,
            spotlight_member=spotlight_member, resonance_link=resonance_link))
    if not title or not nodes:
        return None
    nodes.sort(key=lambda x: x.position)
    return StoryArc(arc_title=title, nodes=nodes[:max_nodes])


def tag_taste_match(arc: StoryArc, candidate_pools: dict[str, list]) -> StoryArc:
    """純函式。逐節點檢查 (artist, song) 是否真的在對應候選池內（spotlight 節點查
    該成員池，主線節點查 shared 池），標記到 `taste_match`——**純資訊性標記，不砍節點**。

    LLM 可以自由推薦候選池外的「驚喜」歌（`taste_match=False`），存在性/可播性交給
    `resolve_story_arc`（resolve-then-VERIFY）把關，這裡只回答「這首歌是不是這群人
    已知聽過/喜歡的」，供人審參考。比對用 `_norm_for_match` 正規化，避免大小寫/空白
    差異誤殺。

    同時把 position **重新編號成連續 1..N**（`parse_story_outline` 若丟過缺欄位節點
    會留下缺口，例如 1,3——這個缺口會讓 Call 2 的 `build_interjection_prompt` 用非
    連續 position 當 key，觀察到 LLM 常自己按順序 1..N 回而非照給的缺口編號回，造成
    `curate_story_interjections` 對錯／漏配。重新編號後 Call 2 收到的 position 一定
    連續，跟 LLM 自然傾向的編號方式一致）。
    """
    def _pool_has(pool: list, artist: str, song: str) -> bool:
        na, ns = _norm_for_match(artist), _norm_for_match(song)
        return any(_norm_for_match(c.anchor_artist) == na and _norm_for_match(c.anchor_title) == ns
                  for c in pool)

    tagged = []
    for i, node in enumerate(arc.nodes, 1):
        pool_key = node.spotlight_member if node.spotlight_member else "shared"
        pool = candidate_pools.get(pool_key, [])
        match = _pool_has(pool, node.artist, node.song)
        tagged.append(replace(node, position=i, taste_match=match))
    return StoryArc(arc_title=arc.arc_title, nodes=tagged)


async def curate_story_outline(brief: StoryBrief | None, candidate_pools: dict[str, list],
                               exclude_titles: list[str], *, call_fn=None) -> StoryArc | None:
    """Call 1 協調：build outline prompt → call LLM（注入 call_fn）→ parse → 驗證選歌。

    brief=None / LLM 失敗 / 解析失敗 → 回 None（caller fallback，不中斷音樂）。
    call_fn 預設 llm_pool.call_paid_review（走 bus 付費池、JSON mode、thinking off）。
    """
    if brief is None:
        return None
    if call_fn is None:
        from llm_pool import call_paid_review
        call_fn = call_paid_review
    system, user = build_outline_prompt(brief, candidate_pools, exclude_titles)
    try:
        resp = await call_fn(user, system=system, caller="dj_story_arc_outline")
    except Exception:
        return None
    arc = parse_story_outline(resp, max_nodes=brief.node_count)
    if arc is None:
        return None
    return tag_taste_match(arc, candidate_pools)


# ── Step 5: Call 2 — 口白（拿到定案歌單才寫）─────────────────────────────────

_INTERJECTION_SYS = (
    "你是一位懂這群人的 DJ。下面是已經定案的一段故事歌單（順序固定），幫每首歌寫一段"
    "節目口白（interjection_script），串成一個連貫的故事。\n"
    "規則：\n"
    "1) 口白要做真正的【過渡】：承接上一首歌收尾的情緒，開啟這一首歌。不是每首歌各自"
    "獨立的前導白，是整段故事的一部分。\n"
    "2) 口白只能用給的真實素材（生活核心句/喜歡過的東西/對話），不確定的細節、沒出現過的"
    "事實絕對不要編——寧可講得籠統，也不要捏造。\n"
    "3) 【語氣鐵則】提到任何人的具體事件時，語氣一律是溫暖共鳴，絕對不准針對、roast、"
    "嘲諷、翻舊帳、拿來調侃當事人——即使素材聽起來像糗事，也要往『這段經歷讓人有共鳴』"
    "的方向寫，讓當事人聽了會覺得被理解，不是被消遣。\n"
    "4) 節點若標了 spotlight_member，口白要聚焦在他的個人回憶/喜好，不能講成好像是"
    "別人也經歷過的。節點若標了 resonance_link，口白要講清楚『這是誰的事』『這讓我想到"
    "誰你好像也...』，絕對不能把兩人的事混成同一件事講。\n"
    '只回 JSON：{{"scripts":[{{"position":1,"interjection_script":"…"}}, ...]}}'
)


def build_interjection_prompt(arc: StoryArc, brief: StoryBrief) -> tuple[str, str]:
    """純函式 → (system, user)。Call 2：已定案的歌單順序 + 共同/個人核心句組進 user。"""
    shared_cores_text = "\n".join(f"- {c}" for c in brief.shared_cores) or "（無）"
    member_blocks = []
    for m, cs in brief.member_cores.items():
        lines = "\n".join(f"  - {c}" for c in cs) or "  （無）"
        member_blocks.append(f"【{m}的個人回憶】\n{lines}")
    member_text = "\n".join(member_blocks) or "（無）"
    liked = "、".join(brief.liked_items) or "（無）"
    node_lines = []
    for n in arc.nodes:
        spotlight = f"spotlight={n.spotlight_member}" if n.spotlight_member else "spotlight=無(共同主線)"
        resonance = f"resonance_link={n.resonance_link}" if n.resonance_link else ""
        node_lines.append(
            f"  {n.position}. {n.artist} - {n.song}（{n.emotion_tag or '?'}｜{spotlight}"
            f"{'｜' + resonance if resonance else ''}）")
    nodes_text = "\n".join(node_lines) or "（無節點）"
    user = (
        f"《{arc.arc_title}》已定案歌單（依序）：\n{nodes_text}\n\n"
        f"【共同回憶】：\n{shared_cores_text}\n\n"
        f"{member_text}\n\n"
        f"最近喜歡過的東西：{liked}\n\n"
        f"請幫每個節點寫口白，回 JSON。"
    )
    return _INTERJECTION_SYS, user


def parse_story_interjections(resp: str) -> dict[int, str] | None:
    """純函式。Call 2 的 LLM JSON → {position: interjection_script}；空/壞/無 scripts → None。"""
    if not resp:
        return None
    m = re.search(r"\{.*\}", resp, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    raw = d.get("scripts") if isinstance(d.get("scripts"), list) else []
    out: dict[int, str] = {}
    for s in raw:
        if not isinstance(s, dict):
            continue
        try:
            position = int(s.get("position"))
        except (TypeError, ValueError):
            continue
        script = str(s.get("interjection_script", "")).strip()
        if script:
            out[position] = script
    return out or None


async def curate_story_interjections(arc: StoryArc | None, brief: StoryBrief, *,
                                     call_fn=None) -> StoryArc | None:
    """Call 2 協調：build interjection prompt → call LLM（注入 call_fn）→ parse → 合併回節點。

    arc=None/無節點 → 原樣回傳（不呼叫 LLM）。LLM 失敗/解析失敗/某節點沒拿到口白 →
    該節點 interjection_script 維持空字串（歌是好的仍可播，只是播報比較平淡，人審看得到），
    不丟節點、不中斷整條弧。
    """
    if arc is None or not arc.nodes:
        return arc
    if call_fn is None:
        from llm_pool import call_paid_review
        call_fn = call_paid_review
    system, user = build_interjection_prompt(arc, brief)
    try:
        resp = await call_fn(user, system=system, caller="dj_story_arc_interjection")
    except Exception:
        return arc
    scripts = parse_story_interjections(resp)
    if not scripts:
        return arc
    new_nodes = [replace(n, interjection_script=scripts.get(n.position, n.interjection_script))
                for n in arc.nodes]
    return StoryArc(arc_title=arc.arc_title, nodes=new_nodes)


# ── resolve + 記錄（不變）─────────────────────────────────────────────────────

async def resolve_story_arc(arc: StoryArc, *, resolve_fn, exclude_vids=None,
                            is_non_song_fn=None, extract_vid_fn=None,
                            verify_title: bool = True) -> list[dict]:
    """StoryArc 的每個 node → resolve(artist+song) → 過品質閘 → enqueue-ready info dicts。

    候選池只給了歷史 title/artist，沒有現成可播 URL（`music_recommender.Candidate`
    的 `direct_url` 只有 T2 discovery 那條路徑才填），所以仍要靠 yt-dlp query resolve。

    每個 info 帶 `_story_arc_title`/`_story_node_position`/`_story_interjection_script`/
    `_story_emotion_tag`/`_story_bpm_target`/`_story_volume_delta_db`/`_story_song_query`/
    `_story_spotlight_member`/`_story_resonance_link`/`_story_taste_match`。
    resolve 不到/丟例外/非單曲/已播 vid/arc 內重複 → 丟掉，不中斷其餘節點（回傳長度可能
    < len(arc.nodes)）。全注入式（同 themed_playlist.resolve_themed_set 設計）→ 可測、
    不耦合 music_cog。
    """
    exclude_vids = set(exclude_vids or ())
    seen_vids: set[str] = set()
    out: list[dict] = []
    for node in arc.nodes:
        query = f"{node.artist} {node.song}".strip()
        if not query:
            continue
        try:
            info = await resolve_fn(query)
        except Exception:
            continue
        if not info:
            continue
        if verify_title and not _resolved_title_matches(node.song, info.get("title", "")):
            continue
        if is_non_song_fn is not None:
            rejected, _reason = is_non_song_fn(info.get("title", ""), info.get("duration"))
            if rejected:
                continue
        vid = None
        if extract_vid_fn is not None:
            vid = extract_vid_fn(info.get("webpage_url") or info.get("url") or "")
        if vid and (vid in exclude_vids or vid in seen_vids):
            continue
        if vid:
            seen_vids.add(vid)
        info["_story_arc_title"] = arc.arc_title
        info["_story_node_position"] = node.position
        info["_story_interjection_script"] = node.interjection_script
        info["_story_emotion_tag"] = node.emotion_tag
        info["_story_bpm_target"] = node.bpm_target
        info["_story_volume_delta_db"] = node.volume_delta_db
        info["_story_song_query"] = node.song_query
        info["_story_spotlight_member"] = node.spotlight_member
        info["_story_resonance_link"] = node.resonance_link
        info["_story_taste_match"] = node.taste_match
        out.append(info)
    return out


def _resolved_title_matches(song: str, resolved_title: str) -> bool:
    """resolve-then-VERIFY，同 themed_playlist._resolved_title_matches。"""
    ns = _norm_for_match(song)
    if len(ns) < 2:
        return True
    return ns in _norm_for_match(resolved_title)


def build_story_arc_record(arc_title: str, infos: list[dict], *,
                           target_duration_s: float, ts: float,
                           narrative_day: str = "", intro: ShowIntro | None = None) -> dict:
    """純函式。把『實際 resolve 成功』的 infos → 一筆日記 record。

    actual_duration_s 只加總有 numeric duration 的 info（resolve 殘缺的當 0，不炸整批）。
    """
    nodes: list[dict] = []
    actual_duration_s = 0.0
    for info in infos:
        title = str(info.get("title") or "").strip()
        if not title:
            continue
        duration = info.get("duration")
        if isinstance(duration, (int, float)):
            actual_duration_s += float(duration)
        nodes.append({
            "position": info.get("_story_node_position"),
            "title": title,
            "interjection_script": str(info.get("_story_interjection_script") or "").strip(),
            "emotion_tag": str(info.get("_story_emotion_tag") or "").strip(),
            "spotlight_member": info.get("_story_spotlight_member"),
            "resonance_link": info.get("_story_resonance_link"),
            "taste_match": info.get("_story_taste_match"),
            "url": str(info.get("webpage_url") or info.get("url") or "").strip(),
        })
    return {
        "ts": ts, "arc_title": arc_title, "narrative_day": narrative_day,
        "target_duration_s": target_duration_s,
        "actual_duration_s": actual_duration_s,
        "intro": {"script": intro.intro_script, "music_path": intro.intro_music_path}
                if intro is not None else None,
        "nodes": nodes,
    }


def record_story_arc(arc_title: str, infos: list[dict], *, target_duration_s: float,
                     ts: float, narrative_day: str = "", intro: ShowIntro | None = None,
                     path: str = _STORY_ARC_LOG) -> dict:
    """Append 一行故事弧 record 到 records/dj_story_arcs.jsonl。

    遙測寫檔絕不可炸斷音樂 → 全 try/except。無 nodes 不寫。回傳 record 供呼叫端/測試檢視。
    """
    rec = build_story_arc_record(arc_title, infos, target_duration_s=target_duration_s,
                                 ts=ts, narrative_day=narrative_day, intro=intro)
    if not rec["nodes"]:
        return rec
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


# ── 待播節目（Prepare/Play 兩階段拆分）───────────────────────────────────────
#
# 「預先生成」（Prepare）跟「播放」（Play）是兩個獨立時機——Prepare 階段把 LLM 生成
# +TTS 預渲染都做完，存成一份「待播節目」；Play 階段單純讀檔播放，不再做任何生成/
# 合成，播放當下零延遲、可排程在生成完成後的任何時間點觸發（含跨 bot 重啟——
# `records/` 是持久化路徑，不是 process 記憶體）。

DEFAULT_STAGED_SHOW_PATH = "records/dj_story_arc_staged.json"


def build_staged_show(infos: list[dict], intro: ShowIntro, *, intro_audio_path: str | None,
                      intro_audio_duration_s: float, ts: float, narrative_day: str = "",
                      target_duration_s: float = 0.0) -> dict:
    """純函式。把 resolve 完、且口白 TTS 已預渲染的 infos + intro → 一份可持久化的
    「待播節目」dict。

    歌曲本身不重新設計 schema——直接原樣保留 `resolve_story_arc` 產出的 info dict
    （含 `url`/`webpage_url`/`duration`/`highlight_start_s` 這些播放要用的原始欄位，
    以及 `_story_*` 標記），播放時直接丟進現有 `stream_queue` 給 `_stream_loop`/
    `_run_tail_dj` 接手，不要另外重造一套只挑幾個欄位存的簡化格式——2026-08-17
    真機測試踩到的三個 bug（still_active/BGM音量/webpage_url不是可播網址）本質上都是
    自己重造播放邏輯繞開既有正確實作造成的，別再犯。

    每個 info 除了既有 `_story_*` 欄位外，還要帶 `_story_interjection_audio_path`/
    `_story_interjection_duration_s`（呼叫端預渲染完才會有；沒有代表那段口白沒有
    語音，播放時 `_fetch_dj_interjection_raw` 的 story_arc 分支會優雅跳過，只播歌
    不播口白，不中斷整場）。
    """
    return {
        "ts": ts,
        "arc_title": infos[0].get("_story_arc_title", "") if infos else "",
        "narrative_day": narrative_day,
        "target_duration_s": target_duration_s,
        "intro": {
            "script": intro.intro_script, "music_path": intro.intro_music_path,
            "audio_path": intro_audio_path, "audio_duration_s": intro_audio_duration_s,
        },
        "infos": list(infos),
    }


def save_staged_show(staged: dict, *, path: str = DEFAULT_STAGED_SHOW_PATH) -> None:
    """原子寫（tmp+replace）——待播節目是單一可變狀態（隨時只有一份「準備好要播的
    下一場」），不是像 `dj_story_arcs.jsonl` 那樣的歷史記錄，覆蓋舊的即可。fail-open：
    寫檔失敗不炸斷 Prepare 流程（caller 仍會拿到記憶體內的 staged dict 可用）。"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(staged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def load_staged_show(*, path: str = DEFAULT_STAGED_SHOW_PATH) -> dict | None:
    """讀待播節目；沒有/壞檔 → None（caller 告知使用者「沒有準備好的節目，先
    Prepare」，不是自動回頭生成——Prepare/Play 分離就是要播放當下零 LLM/TTS 延遲）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_staged_show(*, path: str = DEFAULT_STAGED_SHOW_PATH) -> None:
    """播完就清掉——避免同一場節目被重複播放，或下次 Play 誤讀到上一場殘留。
    fail-open：刪檔失敗不影響已經播完的事實，只是留了個檔案，下次 Prepare 會覆蓋它。"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
