"""v0 種子題庫 — 只有一題 hardcode。

v2 會擴成 JSON bank loader。在那之前保持極簡。
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Puzzle:
    """海龜湯題目資料結構。

    surface  玩家可見的謎題表面
    truth    完整真相（只有 Marvin/LLM 看得到）
    key_facts 構成正確答案的關鍵事實清單；玩家的「最終猜答」需 cover ≥ 2 個核心
              （索引 0 和 1）才算通過
    leak_keywords narration 後處理的禁用詞，命中且問題不含時改寫
    hints    手寫提示，由弱到強排序。玩家或 idle timer 觸發時依序提供
    """
    id: str
    surface: str
    truth: str
    key_facts: list[str] = field(default_factory=list)
    leak_keywords: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


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
        "男子是侏儒（或身材矮小）",                  # index 0：核心，必須命中
        "電梯按鈕的高度問題 / 他構不到 22 樓按鈕",   # index 1：核心，必須命中
        "18 樓是他能按到的最高樓層",                 # index 2：bonus
        "早上下樓沒問題（能按到 1 樓）",             # index 3：bonus
        "有人陪同搭電梯時可以直達 22 樓",            # index 4：bonus
    ],
    leak_keywords=["侏儒", "矮", "身材", "按鈕", "夠不到", "構不著", "按不到"],
    hints=[
        # 由弱到強，依「聯想維度」遞進。產自 scripts/generate_puzzle_hints.py 並由作者挑選。
        # 1D 直接關聯：指向「身體限制」這個類別
        "想想他身體上的限制會怎麼影響日常動作",
        # 2D 二維關聯：對比下樓 / 上樓兩個情境差異
        "為什麼他能下到 1 樓卻上不了 22 樓？這差在哪？",
        # 3D 三維關聯：點出「依賴條件」這個機制
        "有別人一起時能到頂樓，自己卻不行 — 這依賴什麼條件？",
    ],
)


V0_DEFAULT_PUZZLE = ELEVATOR_18F


def get_default_puzzle() -> Puzzle:
    return V0_DEFAULT_PUZZLE
