"""v0 種子題庫 — 只有一題 hardcode。

v0.5：hint 從線性 list 升級為「節點 + 揭露關係」網。
- HintNode：單一可揭露的事實節點（atomic insight）
- Hint：提示文字 + 它揭露哪些節點（reveals）
- 同一節點可被多條 hint 共用
- Hint 不再強制 1D/2D/3D 三層，可以有任意 depth，順序由 puzzle.hints 決定

v2 會擴成 JSON bank loader。在那之前保持極簡。
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HintNode:
    """單一可揭露的事實節點。屬於湯底推理鏈中的一環。

    id        節點唯一識別碼（給 Hint.reveals 引用）
    fact      人類可讀的事實描述（內部用，不直接給玩家看）
    keywords  玩家問題中含這些詞 → 視為玩家已「探索」此節點，
              引擎不再重複給包含此節點的 hint（個人化排序用）
    """
    id: str
    fact: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hint:
    """提示文字 + 它揭露哪些節點。

    text     玩家會聽到的提示語
    reveals  此提示揭露的 HintNode.id 清單（tuple 因為 frozen dataclass）

    depth = len(reveals) 推導，越深的 hint 揭露越多節點。
    順序由 puzzle.hints 的 list 順序定（作者決定給的次序）。
    """
    text: str
    reveals: tuple[str, ...] = ()


@dataclass(frozen=True)
class Puzzle:
    """海龜湯題目資料結構（v0.5 graph 版）。

    surface       玩家可見的謎題表面
    truth         完整真相（只有 Marvin/LLM 看得到）
    key_facts     最終猜答的判定基準，玩家答案需 cover key_facts[0] 與 [1] 才算對
    leak_keywords narration 後處理的禁用詞，命中且問題不含時改寫
    hint_nodes    推理鏈中所有可揭露的事實節點
    hints         hint 清單，每條揭露不同節點子集；list 順序 = 給的先後
    """
    id: str
    surface: str
    truth: str
    key_facts: list[str] = field(default_factory=list)
    leak_keywords: list[str] = field(default_factory=list)
    hint_nodes: list[HintNode] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)

    def hint_node_by_id(self, node_id: str) -> HintNode | None:
        for n in self.hint_nodes:
            if n.id == node_id:
                return n
        return None


# ── ELEVATOR_18F: 電梯到 18 樓的男人（侏儒題）─────────────────────────────────
#
# 推理網：
#
#   body_limit ────► button_reach ────► assist_dependence
#   (身體限制)      (按鈕觸及範圍)    (依賴別人幫忙)
#                                          │
#                       ┌──────────────────┴───────────────────┐
#                       ▼                                       ▼
#                 morning_works                          evening_blocked
#               (早上能按到 1 樓)                    (晚上獨自只能到 18 樓)
#
# 三條 hint 依「依賴深度」遞進，但各自覆蓋的節點子集是作者設計的「網點」。
#
ELEVATOR_18F = Puzzle(
    id="elevator_18f",
    surface=(
        "男子住在大廈 22 樓。每天他出門上班時搭電梯直達 1 樓。"
        "下班回家時，他只搭電梯到 18 樓，然後走樓梯走完最後 4 層回到 22 樓。"
        "他沒有運動需求，電梯也沒壞。為什麼他要這樣？"
    ),
    truth=(
        "男子是侏儒，身高只夠按到電梯按鈕的 18 樓位置。"
        "早上下樓沒問題，因為他能按到最低的 1 樓。"
        "晚上回家若電梯裡剛好有別人，他可以拜託對方幫他按 22 樓直達；"
        "但他獨自搭電梯時，只能按到他構得到的最高樓層 18 樓，"
        "剩下 4 層只好走樓梯。"
    ),
    key_facts=[
        "男子是侏儒（或身材矮小）",
        "電梯按鈕的高度問題 / 他構不到 22 樓按鈕",
        "18 樓是他能按到的最高樓層",
        "早上下樓沒問題（能按到 1 樓）",
        "有人陪同搭電梯時可以直達 22 樓",
    ],
    leak_keywords=["侏儒", "矮", "身材", "按鈕", "夠不到", "構不著", "按不到"],
    hint_nodes=[
        HintNode(
            id="body_limit",
            fact="男子身體有不尋常的限制",
            keywords=("身高", "身材", "身體", "矮", "侏儒", "個子", "高度"),
        ),
        HintNode(
            id="button_reach",
            fact="某些電梯按鈕在他能力範圍外",
            keywords=("按鈕", "按鍵", "夠到", "夠不到", "構到", "構不到", "按不到"),
        ),
        HintNode(
            id="assist_dependence",
            fact="獨自時辦不到，有人在場時可以",
            keywords=("別人", "朋友", "鄰居", "陪同", "幫忙", "獨自", "一個人", "自己"),
        ),
    ],
    hints=[
        # 第 1 條：1 個節點（身體層次）
        Hint(
            text="想想他身體上的限制會怎麼影響日常動作",
            reveals=("body_limit",),
        ),
        # 第 2 條：2 個節點（身體 + 對比情境）
        Hint(
            text="為什麼他能下到 1 樓卻上不了 22 樓？這差在哪？",
            reveals=("body_limit", "button_reach"),
        ),
        # 第 3 條：3 個節點（身體 + 按鈕 + 依賴條件）
        Hint(
            text="有別人一起時能到頂樓，自己卻不行 — 這依賴什麼條件？",
            reveals=("body_limit", "button_reach", "assist_dependence"),
        ),
    ],
)


V0_DEFAULT_PUZZLE = ELEVATOR_18F


def get_default_puzzle() -> Puzzle:
    return V0_DEFAULT_PUZZLE
