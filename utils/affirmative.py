"""
affirmative.py — 判斷 STT 轉錄文字是否為肯定回覆。

用於 ConfirmationContext：Marvin 問「要話題嗎？」後的 30 秒語音偵測視窗。
"""

# 否定前綴：只要文字以這些開頭，直接回傳 False（不管後面有沒有肯定詞）
_NEGATIVE_PREFIXES = (
    "不",
    "沒",
    "算了",
    "no ",
    "No ",
    "no\t",
)

# 嚴格否定詞（完全相符或獨立的否定）
_EXACT_NEGATIVES = {
    "不",
    "no",
    "No",
    "NO",
    "算了",
    "沒關係",
}

# 肯定關鍵字（contains 比對）
_AFFIRMATIVE_KEYWORDS = [
    "要",
    "好",
    "可以",
    "行",
    "嗯",
    "yeah",
    "ok",
    "yes",
]


def is_affirmative(text: str) -> bool:
    """
    判斷 STT 轉錄文字是否為肯定回覆。

    Args:
        text: STT 轉錄的原始文字（可能含有額外的字）

    Returns:
        True 表示肯定（要、好、可以、ok…），False 表示否定或無關。
    """
    stripped = text.strip()
    if not stripped:
        return False

    # 1. 嚴格否定詞：完全符合
    if stripped in _EXACT_NEGATIVES:
        return False

    # 2. 否定前綴過濾：文字開頭含否定就直接 False
    #    - 「不要」「不行」「不好」「不用了」等
    #    - 「沒關係」已在 _EXACT_NEGATIVES，但「沒…」其他句子也排除
    if stripped.startswith("不") or stripped.startswith("沒"):
        return False

    # 3. 「no」獨立判斷（不區分大小寫），但需排除「no」出現在句子開頭做否定
    #    例：「no thanks」→ False；「ok no」→ 視為肯定（ok 先命中）
    lower = stripped.lower()
    if lower == "no":
        return False
    # 「no 」或「no\t」開頭代表否定前綴（例：「no thanks」）
    if lower.startswith("no ") or lower.startswith("no\t"):
        return False

    # 4. 肯定關鍵字 contains 比對（不區分大小寫用於英文）
    for kw in _AFFIRMATIVE_KEYWORDS:
        if kw.lower() in lower or kw in stripped:
            return True

    return False
