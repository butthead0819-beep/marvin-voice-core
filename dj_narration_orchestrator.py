"""DJ 串場「這輪要不要講、講什麼」協調順序的顯式化入口。

## 背景

「DJ 在歌與歌之間要講什麼話」這件事，實際邏輯散落在至少 8 個獨立檔案
（dj_topic_selector / dj_life_context / dj_social_affinity /
dj_comedy_fallback / dj_prompt_builder / dj_tail_schedule / joke_bank），
真正的呼叫順序則寫死在 `cogs/music_cog_tail_dj.py::_run_tail_dj` 跟
`cogs/music_cog_dj_lyrics.py::_fetch_dj_interjection_raw` 兩支方法的程式碼裡，
要理解「Marvin 這輪為什麼講了這句話」得自己讀 8 個檔案拼順序。

第 3 刀起，口白 mode 的決策（優先序鏈＋扭蛋池＋覆蓋層）與各 mode 的
ctx 段落由本模組的 MODES 註冊表驅動；文字 cascade 與 TTS 仍在 cog。

## 決策圖

```
DJMaterials ──► forced 層（order）：revival → memory_match → song(focus)
                 └─ 命中即回傳，不進扭蛋（不碰 life/interest 冷卻）
             ──► gacha 層：委派 dj_topic_selector.select_mode（mark_used + last_fallback）
             ──► override 層：reason.override_when(chosen ∈ {quick, atmosphere} 且 autopilot_reason)
             ──► NarrationPlan(mode, topic, ctx_lines, tts_emotion, side_effects)
apply_side_effects(plan)  ← 同步 def，緊接 plan_narration，無 await（PR #102）
   ├─ consume_conversation：is_consumed 重查 → 搶光則 downgrade(plan,"quick")（不寫冷卻）
   └─ consume_callback
select_narration_mode(...) = 薄包裝 → choose_mode → (topic, mode)   [D13]
加一個扭蛋 mode：MODES 一筆 + DJMaterials 一欄 + cog 收集一行 + select_mode 一個參數。
```
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Collection, Literal

from dj_tail_schedule import tail_dj_fire_delay
from dj_topic_selector import TopicCooldownStore, select_mode

logger = logging.getLogger(__name__)

# 與 cogs/music_cog_tail_dj.py 的 _DJ_TAIL_LEAD_S 同值——尾段疊播窗口寬度，
# 不在這裡重新定義成 import（避免跟主檔互相 import 造成循環，同該檔案
# docstring 講的 _get_puck_client() local import 理由），呼叫端仍應自己
# 傳目前生效的 lead_s，這裡的預設值只是方便單獨測試/探索時有個合理起點。
_DEFAULT_TAIL_LEAD_S = 8.0


def compute_tail_fire_delay(
    duration_s: float | None,
    elapsed_s: float,
    *,
    highlight_start_s: float | None = None,
    lead_s: float = _DEFAULT_TAIL_LEAD_S,
) -> float | None:
    """[Step 1] 這輪尾段串場「要不要點火、還要等幾秒」——原樣包 `_run_tail_dj`
    開頭那段時間軸換算，只是把它從方法內的一段程式碼抽成獨立可測的函式。

    duration 未知（None/0）直接放棄（呼叫端該退回舊行為，見
    `_run_tail_dj` 開頭的 early return + log）。有 highlight_start_s
    （精華起播位移了實際播放時間軸）時，duration 要先扣掉這段位移，
    否則會把「離結尾還有多久」算得太樂觀——這行為原樣照抄
    `_run_tail_dj`，不是這裡新想的。

    真正「還要等幾秒才點火」的計算全權交給 `dj_tail_schedule.tail_dj_fire_delay`
    （歌太短 / 已經過窗一樣回 None），這裡不重算一份。

    回傳 None 時，呼叫端該做的事跟 `_run_tail_dj` 一樣：整輪放棄尾段串場，
    退回「混進下一首開頭」或 `_maybe_play_dj_interjection` 的既有路徑
    ——這件事本身仍是呼叫端的責任，這個函式只回答「現在」要不要點火。
    """
    if not duration_s:
        return None
    if highlight_start_s:
        duration_s = max(0.0, duration_s - highlight_start_s)
    return tail_dj_fire_delay(duration_s, elapsed_s, lead_s=lead_s)


RARE_MAX_PLAYS = 2  # 10/4 使用者定：全伺服器 ≤2 次＝新歌/少播


def autopilot_narration_focus(info: dict) -> str:
    """autopilot 選的歌依熟悉度決定口白方向：'silent'（懷舊位不講）/'song'（少播→講歌本身）/
    'topic'（熟歌→串話題）；非 autopilot 或缺播放次數回 ''（維持原扭蛋行為）。

    10/4 使用者定：懷舊位聽的人知道是什麼歌，「幾週前播過」沒意義，直接接歌不打斷節奏；
    新歌/少播多講歌曲本身；其他熟悉的歌串話題。真人點歌不套用。
    """
    if not (info.get('requested_by') or '').startswith('Marvin'):
        return ""
    if info.get('_arc_role') == 'nostalgia':
        return "silent"
    plays = info.get('_server_plays')
    if plays is None:
        return ""
    return "song" if plays <= RARE_MAX_PLAYS else "topic"


@dataclass(frozen=True)
class DJMaterials:
    """一輪串場的全部素材（_fetch_dj_interjection_raw 的 IO 段一次收齊）。"""
    revival_lines: list = field(default_factory=list)
    memory_evidence: str = ""
    focus: str = ""
    life: list = field(default_factory=list)
    present_members: set | None = None
    interests: list = field(default_factory=list)
    news_items: list = field(default_factory=list)
    callbacks: list = field(default_factory=list)
    activities: list = field(default_factory=list)
    has_guide: bool = False
    has_conversation: bool = False
    autopilot_reason: str = ""
    exclude_modes: Collection[str] = ()
    # 以下只給 render 用（D10：吸收原 RenderCtx）
    env: str = ""
    conv_lines: list = field(default_factory=list)
    guide: str | None = None
    song_card: Any = None
    empathy_hooks: tuple = ()


@dataclass(frozen=True)
class NarrationMode:
    name: str
    tier: Literal["forced", "gacha", "override"]
    order: int = 0
    pick: Callable[[DJMaterials, TopicCooldownStore], tuple | None] | None = None
    render: Callable[[str | None, DJMaterials], list[str]] = lambda topic, m: []
    tts_emotion: str = "normal"
    on_chosen: str | None = None
    override_when: Callable[[str, DJMaterials], bool] | None = None


@dataclass(frozen=True)
class NarrationPlan:
    mode: str
    topic: str | None
    ctx_lines: list
    tts_emotion: str
    side_effects: list


# ── forced 層 pick ───────────────────────────────────────────────────────────

def _pick_revival(m: DJMaterials, store: TopicCooldownStore):
    return (None, None) if m.revival_lines else None


def _pick_memory_match(m: DJMaterials, store: TopicCooldownStore):
    ev = (m.memory_evidence or "").strip()
    if ev and "memory_match" not in m.exclude_modes and store.is_cool(ev):
        return ev, None
    return None


def _pick_song(m: DJMaterials, store: TopicCooldownStore):
    return (None, None) if m.focus == "song" else None


# ── render 函式（逐字照抄 `_fetch_dj_interjection_raw` 現行 if/elif） ────────

def _render_memory_match(topic, m: DJMaterials) -> list[str]:
    return [
        f"【你熟悉他的生活】記憶證據（這首為什麼現在放）：\n・{topic}",
        "開場鉤子：開場直接點名，講出這條記憶證據裡他說過或做過的事；只能講證據裡寫的事實，不准自己補細節或編故事。",
    ]


def _render_life(topic, m: DJMaterials) -> list[str]:
    return [
        f"【你熟悉他的生活】最近生活：\n・{topic}",
        random.choice(m.empathy_hooks),
    ]


def _render_interest(topic, m: DJMaterials) -> list[str]:
    return [
        f"【你熟悉他的生活】在場興趣：\n・{topic}",
        random.choice(m.empathy_hooks),
    ]


def _render_callback(topic, m: DJMaterials) -> list[str]:
    return [
        f"【你熟悉他的生活】他之前說過要做的事：\n・{topic}",
        "開場鉤子：點名順口關心這件事後來怎麼樣了，像老朋友隨口問一句；只能講素材裡寫的事，不准自己補細節、不准替他回答。",
    ]


def _render_activity(topic, m: DJMaterials) -> list[str]:
    return [
        f"【你熟悉他的生活】在場的人現在的 Discord 動態：\n・{topic}",
        "開場鉤子：像注意到朋友正在幹嘛順口一提（例如邊打遊戲邊聽這首），再帶進歌；只能講素材裡寫的遊戲名/狀態文字，不准猜遊戲內容、劇情或他玩得怎樣，狀態文字看不懂就照念、不解讀。",
    ]


def _render_news(topic, m: DJMaterials) -> list[str]:
    return [
        f"最新時事消息：\n・{topic}",
        "開場鉤子：簡潔提及這則時事消息，像電台順帶關心生活一樣，自然引導大家聽下一首歌，不說教、不嚴肅。",
    ]


def _render_conversation(topic, m: DJMaterials) -> list[str]:
    out = []
    if m.conv_lines:
        out.append("【你熟悉他的生活】頻道近期對話：\n" + '\n'.join(m.conv_lines))
    out.append("串場方向：用剛才頻道對話的氣氛自然接過去就好，不用硬掰新話題。")
    return out


def _render_revival(topic, m: DJMaterials) -> list[str]:
    return [
        "【你熟悉他的生活】剛剛大家聊過（原句）：\n" + "\n".join(m.revival_lines),
        "串場方向：現在大家聊天告一段落，接回剛剛的話題延續一下，或丟個輕鬆的問題製造話題感，再帶進這首歌；只能用上面原句裡的內容，不准編造誰說了什麼、不准替人下結論。",
    ]


def _render_atmosphere(topic, m: DJMaterials) -> list[str]:
    return [m.env, "開場鉤子：緊扣現在的時間/地點氛圍切入，像是特別為這一刻準備的，不用硬掰別的話題。"]


def _render_guide(topic, m: DJMaterials) -> list[str]:
    from dj_gacha_narrator import pick_gacha_motivation
    out = [f"導聆素材（查證過的真實資料，只能用這裡寫的事實）：\n{m.guide}"]
    card = m.song_card
    if isinstance(card, dict):
        if card.get("lyric_hook") and isinstance(card["lyric_hook"], dict):
            lh = card["lyric_hook"]
            out.append(f"歌詞靈魂刺點：『{lh.get('quote')}』（{lh.get('subtext')}）")
    gacha = pick_gacha_motivation(card, topic=topic)
    if gacha:
        out.append(gacha.instruction)
        out.append("只能講上面素材裡寫的事實，不准自己補細節或編故事。")
    else:
        out.append("串場方向：把導聆素材濃縮成一兩句，點出這首歌耳朵該聽的地方；只能講素材裡寫的事實，不准自己補細節或編故事。")
    return out


MODES: dict[str, NarrationMode] = {
    "revival": NarrationMode("revival", "forced", order=0, pick=_pick_revival, render=_render_revival),
    "memory_match": NarrationMode("memory_match", "forced", order=1, pick=_pick_memory_match, render=_render_memory_match),
    "song": NarrationMode("song", "forced", order=2, pick=_pick_song),
    "life": NarrationMode("life", "gacha", render=_render_life, tts_emotion="upbeat"),
    "interest": NarrationMode("interest", "gacha", render=_render_interest, tts_emotion="upbeat"),
    "news": NarrationMode("news", "gacha", render=_render_news, tts_emotion="upbeat"),
    "callback": NarrationMode("callback", "gacha", render=_render_callback, on_chosen="consume_callback"),
    "activity": NarrationMode("activity", "gacha", render=_render_activity, tts_emotion="upbeat"),
    "guide": NarrationMode("guide", "gacha", render=_render_guide),
    "conversation": NarrationMode("conversation", "gacha", render=_render_conversation, on_chosen="consume_conversation"),
    "atmosphere": NarrationMode("atmosphere", "gacha", render=_render_atmosphere, tts_emotion="calm"),
    "quick": NarrationMode("quick", "gacha"),
    "reason": NarrationMode(
        "reason", "override", order=0,
        override_when=lambda chosen, m: chosen in ("quick", "atmosphere") and bool(m.autopilot_reason),
    ),
}


def tts_emotion_for(mode: str) -> str:
    m = MODES.get(mode)
    return m.tts_emotion if m else "normal"


def choose_mode(materials: DJMaterials, store: TopicCooldownStore, rng=None) -> tuple[str | None, str]:
    """決策鏈：forced（依 order）→ gacha（委派 select_mode）→ override。"""
    for nm in sorted((x for x in MODES.values() if x.tier == "forced"), key=lambda x: x.order):
        hit = nm.pick(materials, store)
        if hit is not None:
            topic, meme_id = hit
            if topic is not None:
                store.mark_used(topic, meme_id=meme_id)
            return topic, nm.name

    select_kwargs = dict(
        present_members=materials.present_members,
        has_conversation=materials.has_conversation,
        news_items=materials.news_items,
        callbacks=materials.callbacks,
        activities=materials.activities,
        has_guide=materials.has_guide and materials.focus != "topic",
        exclude_modes=materials.exclude_modes,
    )
    if rng is not None:
        select_kwargs["rng"] = rng
    topic, mode = select_mode(materials.life, materials.interests, store, **select_kwargs)

    for nm in sorted((x for x in MODES.values() if x.tier == "override"), key=lambda x: x.order):
        if nm.override_when(mode, materials):
            mode = nm.name
            break

    return topic, mode


def plan_narration(materials: DJMaterials, store: TopicCooldownStore, rng=None) -> NarrationPlan:
    topic, mode = choose_mode(materials, store, rng)
    nm = MODES.get(mode)
    return NarrationPlan(
        mode=mode, topic=topic,
        ctx_lines=nm.render(topic, materials) if nm else [],
        tts_emotion=nm.tts_emotion if nm else "normal",
        side_effects=[nm.on_chosen] if nm and nm.on_chosen else [],
    )


def downgrade(plan: NarrationPlan, mode: str, materials: DJMaterials) -> NarrationPlan:
    nm = MODES[mode]
    return NarrationPlan(
        mode=mode, topic=None, ctx_lines=nm.render(None, materials),
        tts_emotion=nm.tts_emotion, side_effects=[],
    )


def apply_side_effects(
    plan: NarrationPlan, materials: DJMaterials, *,
    conv_entries, heat_bank, callback_src, consume_callback,
) -> NarrationPlan:
    if "consume_conversation" in plan.side_effects and conv_entries:
        try:
            bank = heat_bank()
            entries = [e for e in conv_entries if not bank.is_consumed(e)]
            lines = [f"{e['speaker']}：「{e['text'][:25]}」" for e in entries]
            if entries:
                bank.mark_consumed(entries, time.time())
                plan = replace(plan, ctx_lines=MODES["conversation"].render(
                    plan.topic, replace(materials, conv_lines=lines)))
            else:
                plan = downgrade(plan, "quick", materials)  # 素材被搶光，不硬寫空對話串場
        except Exception:
            pass  # fail-open

    if "consume_callback" in plan.side_effects:
        _cb = callback_src.get(plan.topic)
        if _cb:
            try:
                consume_callback(_cb[0], _cb[1])
            except Exception as e:
                logger.warning(f"⚠️ [DJ Callback] consume 失敗: {e}")

    return plan


def select_narration_mode(
    *,
    life,
    interests,
    topic_store: TopicCooldownStore,
    present_members=None,
    has_conversation: bool = False,
    news_items=None,
    callbacks=None,
    activities=None,
    autopilot_reason: str = "",
    memory_evidence: str = "",
    has_guide: bool = False,
    focus: str = "",
    exclude_modes: Collection[str] = (),
) -> tuple[str | None, str]:
    """[Step 5c] 這輪串場的話題素材從哪來——原樣包
    `_fetch_dj_interjection_raw` 裡「呼叫 dj_topic_selector.select_mode
    →autopilot 理由覆蓋 quick/atmosphere」這兩步的固定組合。

    優先序：記憶對歌（memory_evidence，在場者親口說過喜歡這首歌/歌手的
    具體證據，命中且沒冷卻中就直接勝出，不再進 select_mode）→ 其餘全部交給
    `dj_topic_selector.select_mode` 的扭蛋池（近期生活主角要在場、在場興趣、
    新聞、在場者的 Discord 動態、guide/conversation/
    atmosphere/quick，依各自有沒有素材建池後加權隨機抽一個；真正的挑選邏輯在
    `select_mode` 裡，這裡不重複實作；has_guide=True 時 guide 才會進這輪的候選）。

    memory_evidence 命中時**不呼叫** `select_mode`——否則 life/interest
    話題會被白白 `mark_used` 冷卻掉，等於這輪沒講到卻先燒掉了下次的素材。

    autopilot_reason 覆蓋規則原樣照抄 `_fetch_dj_interjection_raw`：只有
    Marvin 自己選歌才會算出 `_autopilot_pick_reason`；只在 select_mode
    選到 quick 或 atmosphere 這兩個「沒有具體話題可用」的墊底 fallback
    時才蓋掉，換成有憑有據的推薦理由（mode="reason"）——不搶 life/interest/
    news/conversation 這些已經挑到具體
    素材的 mode。

    回傳 (topic_text, mode)，跟 `select_mode` 的回傳形狀一致，mode
    多兩種 "memory_match"/"reason" 是這一步疊加上去的，不是 `select_mode`
    本身會回的值。

    focus（見 `autopilot_narration_focus`，10/4）：memory_match 之後才看。
    'song'（少播）→ 一律回 (None, "song")，不進扭蛋池（不燒話題冷卻）；歌本身的素材
    由呼叫端 `pick_song_facet` 抽（10/4 導聆也是其中一個 facet）；'topic'（熟歌）→ 照常扭蛋
    但不放 guide；''→ 完全照舊。

    exclude_modes（10/4）：同一首歌最近 2 次用過的 mode，memory_match 與扭蛋池都避開
    （見 dj_topic_selector.select_mode）。focus='song' 不受影響。

    第 3 刀起為薄包裝：組 DJMaterials → choose_mode；移除見 TODOS.md。
    """
    return choose_mode(DJMaterials(
        life=life, interests=interests, present_members=present_members,
        has_conversation=has_conversation,
        news_items=news_items, callbacks=callbacks, activities=activities,
        autopilot_reason=autopilot_reason, memory_evidence=memory_evidence,
        has_guide=has_guide, focus=focus, exclude_modes=exclude_modes,
    ), topic_store)


def pick_song_facet(
    available: dict[str, str], *, exclude: Collection[str] = (), rng=random,
) -> tuple[str, str] | None:
    """少播歌只抽歌本身的素材：available＝{facet: 素材文字}（只放有素材的 facet），去掉 exclude 後
    均等隨機挑一個回 (facet, 文字)；全被排除時改從全部 available 挑（素材少的歌也要有話講）；available 空回 None。"""
    if not available:
        return None
    pool = [f for f in available if f not in exclude]
    if not pool:
        pool = list(available)
    facet = rng.choice(pool)
    return facet, available[facet]


def pick_song_material(
    candidates: list[str], *, exclude_text: str = "", exclude: Collection[str] = (), rng=random,
) -> str | None:
    """歌曲素材（喜好線索/情感記錄/歌詞呼應/歌曲資料/選這首的理由）只抽 1 個，
    不再全部無條件疊進 ctx（9/30 使用者定：主素材 1 個 + 歌曲素材 1 個，
    治「素材無條件疊加造成口白混線」——抽到氛圍卻還扯對話裡的床墊）。

    exclude_text 非空時，濾掉「exclude_text 是該行子字串」的候選，避免
    memory_match 的記憶證據跟 affinity 等行重複講兩次同一件事。
    exclude：候選完全等於其中任一字串就去掉（同一首歌最近用過的素材，10/4）。
    """
    pool = [c for c in candidates if c]
    ex = (exclude_text or "").strip()
    if ex:
        pool = [c for c in pool if ex not in c]
    pool = [c for c in pool if c not in exclude]
    if not pool:
        return None
    return rng.choice(pool)


def format_reason_line(who: str, title: str, reason: str) -> str:
    """選歌理由進 context 的固定句型（10/9 使用者定）：「XXX會喜歡這首《歌名》，理由是XXX」。"""
    who = (who or "").strip() or "大家"
    return f"推薦理由：{who}會喜歡這首《{title}》，理由是{reason}"


# [Step 5e 文件化] 見模組開頭「決策圖」——這段優先序目前只存在於
# `_fetch_dj_interjection_raw` 的一連串 if/elif 裡（joke_bank 冷卻時間戳
# 讀寫、LLM 呼叫、TTS 生成都是 side effect，無法在不動那支方法的前提下
# 抽成可 characterization test 的純函式），這裡先用一份唯讀常數把順序
# 「寫下來」，讓下一個改動者不用重新讀一次整支方法才拼得出順序。
NARRATION_TEXT_CASCADE = (
    "joke_bank",        # 非熱聊 + 冷卻已過 + 下一首歌名拼音撞中 hook
    "quick_template",  # mode=="quick" 且以上都沒命中：本地固定模板，零 LLM
    "llm",             # 以上都沒有 → dj_prompt_builder 組 prompt，走 bot.router
    "autopilot_template",  # LLM 空手/不合格 且是 Marvin 自選歌：_autopilot_dj_phrase 模板池
    "fixed_announcement",  # 以上皆無效的保底：「DJ Marvin為你帶來《X》」固定格式，永不失敗
)
