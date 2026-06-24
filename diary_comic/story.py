"""故事編排：把 10 分鐘日誌（骨幹）+ 精華（高潮）融合成一頁漫畫的故事計畫。

設計（2026-06-21 與 Jack 定）：
- 條漫 off。有精華才出（沒高潮不畫）。
- 豐富（≥6 筆 context）→ 日漫 4 格：物件 context + Hero 斜切拆兩拍（鋪哏→爆笑）+ 標題 + 馬文。
- 薄 → 一格 meme：強反差單飛、反差中才 Marvin 救援。
- arc 編排：最強笑點當高潮（Hero），前面墊 context。

純函式（不碰 API）。實際出圖/清理/標題在 render 端注入 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from diary_comic.parser import DiaryEntry, dedupe_adjacent
from diary_comic.highlight import (
    Highlight, highlight_to_entry, meme_needs_marvin, _setup_text)

# 樣板輪替池：內容分層(衝/穩) + 層內日期輪，避免每天同一版面
_PUNCHY = ("T2", "T4")  # 夠強：T2 頂爆鉤子 / T4 中央爆+余韵
_STEADY = ("T1", "T3")  # 普通：T1 建勢底爆 / T3 純方正三拍

# 每樣板的列高比例（手調，鎖整頁 9:16，讓每格長寬比不過扁；總和=1）
TEMPLATE_HEIGHTS = {
    "T1": [0.27, 0.31, 0.42],  # pair / 中景 / Hero
    "T2": [0.42, 0.31, 0.27],  # Hero / 中景 / pair
    "T3": [0.28, 0.30, 0.42],  # 遠景 / 中景 / Hero
    "T4": [0.26, 0.42, 0.32],  # 遠景 / Hero / pair(反應)
}

MIN_CONTEXT = 6  # ≥ 這麼多筆 → 漫畫；否則 meme


def choose_format(diary_session, highlights) -> str | None:
    """meme / slant / None。沒精華→None；豐富→slant；薄→meme。

    省錢：用**話題變化數**（去掉沒變化的跳針條目）判豐富，不是原始筆數。
    整小時聊同一件事 → 去重後變薄 → 降級 meme（1 圖）不出 slant（多圖）。
    """
    if not highlights:
        return None
    varied = dedupe_adjacent(diary_session)  # 沒話題變化的不計入
    return "slant" if len(varied) >= MIN_CONTEXT else "meme"


@dataclass
class StoryPlan:
    format: str                                   # "meme" | "slant"
    highlight: Highlight                           # 高潮精華
    context: list[DiaryEntry] = field(default_factory=list)  # 物件 context（slant）
    peak_setup: DiaryEntry | None = None           # Hero 上格：鋪哏
    peak_reaction: DiaryEntry | None = None        # Hero 下格：爆笑
    meme_top: str = ""                             # meme 上文字（鋪哏）
    meme_bottom: str = ""                          # meme 下文字（Marvin 或空）
    needs_marvin: bool = False                     # meme 是否要 Marvin 救援


def fuse(diary_session, highlights, *, max_context: int = 2) -> StoryPlan | None:
    """融合成故事計畫。回 None = 不出。"""
    fmt = choose_format(diary_session, highlights)
    if fmt is None:
        return None
    peak = max(highlights, key=lambda h: h.strength)  # 最強笑點當高潮

    if fmt == "meme":
        need = meme_needs_marvin(peak)
        return StoryPlan(format="meme", highlight=peak,
                         meme_top=_setup_text(peak)[:30] or "（鋪哏）",
                         meme_bottom="" if not need else "",  # Marvin 文字 render 端生
                         needs_marvin=need)

    # slant：Hero 拆兩拍 + 物件 context（arc：context 在前、高潮在後）
    setup = highlight_to_entry(peak, core=_setup_text(peak)[:40] or "（鋪哏場景）")
    reaction = DiaryEntry(ts_str=setup.ts_str, core="全場哄堂大笑、爆笑反應",
                          speakers=setup.speakers, aside=peak.laugh_text)
    context = dedupe_adjacent(diary_session)[:max_context]  # 開場+鋪墊（去跳針）
    return StoryPlan(format="slant", highlight=peak, context=context,
                     peak_setup=setup, peak_reaction=reaction)


def choose_template(plan: StoryPlan, *, day_index: int = 0) -> str | None:
    """挑版面樣板。meme→None；slant→內容分層(衝/穩)後層內日期輪。"""
    if plan.format != "slant":
        return None
    pool = _PUNCHY if not meme_needs_marvin(plan.highlight) else _STEADY
    return pool[day_index % len(pool)]


_TITLE_SYS = ("你是漫畫單話命名員。看這頁聊了什麼，取一個好笑、吸睛的單話標題"
              "（繁中、≤12 字、像漫畫章節名）。只回標題。")


def build_title_prompt(cores: list[str]) -> tuple[str, str]:
    bullets = "、".join(c for c in cores if c)
    return _TITLE_SYS, f"這頁的內容：{bullets}\n\n單話標題："


# meme 文字框架：給模板菜單 + slot，LLM 框內挑模板填詞（不全自由、不亂掰）
_MEME_SYS_BASE = (
    "你是 meme 文字員。看一個語音聊天的爆笑 moment（STT 有雜訊、要還原梗），寫 meme 文字。\n"
    "從這幾個模板挑最合的填詞：\n"
    "A 鋪哏→爆點：top=一本正經的鋪陳、bottom=荒謬結果\n"
    "B 期待 vs 現實：top=以為…、bottom=結果…\n"
    "C 一句金句：top=一句粗體金句、bottom 留空\n"
    "D 標籤式：top=「當你…的時候」\n"
    "規則：繁中、超短、一眼懂、勾內梗、別解釋笑話。\n"
    "{rule}\n"
    '只回 JSON：{{"top": "...", "bottom": "..."}}'
)
_RULE_SOLO = "這梗反差夠大、自己就好笑 → bottom 放梗本身或留空，不要旁白。"
_RULE_MARVIN = "這梗脫離當下有點平 → bottom 放一句馬文式厭世補刀救援它。"


def build_meme_prompt(h: Highlight, *, with_marvin: bool) -> tuple[str, str]:
    system = _MEME_SYS_BASE.format(rule=_RULE_MARVIN if with_marvin else _RULE_SOLO)
    user = f"爆笑 moment：\n{_setup_text(h)}\n（接著大家：{h.laugh_text}）\n\n挑模板填詞，回 JSON："
    return system, user


def parse_meme_text(resp: str) -> tuple[str, str]:
    """解析 LLM 回的 meme 文字 → (top, bottom)。吃 JSON（含 fenced）；非 JSON → 整段當 top。"""
    import json
    import re
    resp = (resp or "").strip()
    if not resp:
        return ("", "")
    m = re.search(r"\{.*\}", resp, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return (str(d.get("top", "")).strip(), str(d.get("bottom", "")).strip())
        except Exception:
            pass
    return (resp[:30], "")


# 故事導演：讀逐字稿窗(主題對話) + 摘要(場景脈絡) → 抓主題、把故事說完整 + 寫 per-panel beats
_STORY_SYS = (
    "你是漫畫故事導演。看一段語音聊天的「逐字稿」（STT 有雜訊、要還原語意，別照搬亂碼）"
    "+ 該段摘要（場景脈絡），寫成清楚的分鏡故事。這段可能是爆笑時刻、也可能是一段有來有往的話題討論。\n"
    "步驟：1) 先抓這段在聊什麼『主題』、來龍去脈（誰提出、怎麼發展、結論或轉折是什麼）"
    "2) 找出高潮那句（最好笑、或最關鍵的反差／轉折）3) 還原前因 4) 依時間順序拆拍。\n"
    "把故事說『完整』（最重要）：讓沒在場的人看完就懂他們在聊什麼、結局是什麼——"
    "別只擷取一個笑點碎片或一句亂碼當全部；主題要貫穿每一拍，不准跳到逐字稿裡沒有的別的話題。\n"
    "張力：這是一頁漫畫不是流水帳，情緒沿拍子往上堆、最後收束。"
    "establish/develop 壓著鋪陳留懸念、setup 把『一本正經』頂到最滿、"
    "punchline 用最大反差／最關鍵那句打下來（鋪得越穩、收得越有力）、最後一拍洩力收尾。"
    "scene 的表情／體態／鏡頭要逐拍升溫：平靜→繃緊→爆發→癱軟；caption 的語氣也要看得出張力在漲。"
    "張力只能靠鏡頭、表情、節奏、反差營造——STT 沒有的情節一律不准加。\n"
    "每拍給：scene（畫面動作：誰／做什麼／表情，具體到能出圖）+ caption（清乾淨的台詞，短口語；"
    "沒台詞留空）。\n"
    "把每個角色寫進他的性格（見卡司人設）；caption 多用他們的真口頭禪 → 一眼認出是誰。\n"
    "規則：不准腦補（STT 沒有的情節別編、不確定就保守）；繁中；caption ≤14 字。\n"
    "拍子角色依序固定：{roles}。\n"
    '只回 JSON：{{"understanding":"兩句話講清楚主題＋發生什麼","title":"今晚精華：…（≤12字）",'
    '"beats":[{{"role":"…","scene":"…","caption":"…"}}]}}'
)
_ROLE_DESC = {
    "establish": "establish 開場全景（誰在、在哪、在幹嘛）",
    "develop": "develop 事情發展（動作推進）",
    "setup": "setup 鋪哏（那人一本正經講的那句）",
    "punchline": "punchline 爆點+全場哄堂反應",
    "aftermath": "aftermath 笑完的余韵反應",
}
_DEFAULT_ROLES = ("establish", "develop", "setup", "punchline")
_TEMPLATE_ROLES = {"T4": ("establish", "setup", "punchline", "aftermath")}


def build_story_prompt(h: Highlight, scene_context: str,
                       template_id: str | None = None) -> tuple[str, str]:
    from diary_comic.character_store import persona_brief
    roles = _TEMPLATE_ROLES.get(template_id, _DEFAULT_ROLES)
    system = _STORY_SYS.format(roles="、".join(_ROLE_DESC[r] for r in roles))
    speakers = list(dict.fromkeys([s for s, _ in h.setup] + [h.laugher]))
    cast = "\n".join(persona_brief(s) for s in speakers if s)
    stt = "\n".join(f"{s}：{t}" for s, t in h.setup) or "（無前情）"
    user = (f"卡司人設：\n{cast}\n\n逐字稿短窗：\n{stt}\n（接著全場：{h.laugh_text}）\n\n"
            f"這段摘要（場景脈絡）：{scene_context or '（無）'}\n\n"
            f"寫 {len(roles)} 拍，回 JSON：")
    return system, user


def parse_story(resp: str) -> dict:
    """解析故事導演回的 JSON → {understanding, title, beats[]}。壞掉降級空殼。"""
    import json
    import re
    empty = {"understanding": "", "title": "", "beats": []}
    resp = (resp or "").strip()
    if not resp:
        return empty
    m = re.search(r"\{.*\}", resp, re.S)
    if not m:
        return empty
    try:
        d = json.loads(m.group(0))
    except Exception:
        return empty
    beats = d.get("beats") if isinstance(d.get("beats"), list) else []
    return {"understanding": str(d.get("understanding", "")).strip(),
            "title": str(d.get("title", "")).strip(),
            "beats": [b for b in beats if isinstance(b, dict)]}
