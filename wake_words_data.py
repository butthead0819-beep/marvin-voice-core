"""單一喚醒詞資料來源。

`wake_detector.WAKE_WORDS_LIST`（完整偵測用清單，含詞組/英文/STT 誤判變體，
用途是「有沒有喚醒」）跟 `wake_intent_gate._WAKE_WORDS`（判斷「剝掉喚醒詞後
是否還有實質內容」用的較小集合）用途不同、內容本來就不完全重疊，不能直接
合併成同一個 list，但都該從這裡衍生，不再各自寫死。

每筆 entry：(word, category, consumers)
  - category：human-readable 分類，純標記用途，不驅動邏輯
      "core"        — 官方詞組/英文拼寫
      "stt_variant" — STT 誤判產生的近音變體
  - consumers：這個詞被哪些模組使用，衍生清單時依此過濾
      "detector" → wake_detector.WAKE_WORDS_LIST
      "gate"     → wake_intent_gate._WAKE_WORDS

`WAKE_WORDS_LIST` 的順序有意義（regex alternation 用，長詞在前），因此本檔
entry 順序即衍生順序，改動前先讀 wake_detector.py 開頭的排序註解。
"""
from __future__ import annotations

WAKE_WORD_ENTRIES: list[tuple[str, str, tuple[str, ...]]] = [
    # 3-syllable（誤觸發率最低——比對時放最前面，優先匹配長詞）
    ("嗨馬文", "core", ("detector",)),
    ("艾馬文", "core", ("detector",)),
    ("艾瑪文", "core", ("detector",)),
    ("阿姨文", "core", ("detector",)),
    ("馬文同學", "core", ("detector",)),
    # English in Chinese context（辨識度高）
    ("hey marvin", "core", ("detector",)),
    ("oh marvin", "core", ("detector",)),
    ("marvin", "core", ("detector", "gate")),
    ("marv", "core", ("detector",)),
    ("marwen", "core", ("detector",)),
    ("mavin", "core", ("detector",)),
    # 2-syllable 主詞
    ("馬文", "core", ("detector", "gate")),
    # STT near-misses
    ("馬聞", "stt_variant", ("detector",)),
    ("馬溫", "stt_variant", ("detector",)),
    ("麻文", "stt_variant", ("detector", "gate")),
    ("馬問", "stt_variant", ("detector",)),
    ("馬穩", "stt_variant", ("detector",)),
    ("馬門", "stt_variant", ("detector",)),
    ("馬萌", "stt_variant", ("detector",)),
    # 2026-06-13 SwiftV2 實測聲學混淆（「馬文這首誰唱的」→「毛文…」喚醒漏接）
    ("毛文", "stt_variant", ("detector",)),
    # 只在 wake_intent_gate 用到的變體（cleaner 注入後可能剩這些）
    ("瑪文", "stt_variant", ("gate",)),
    ("媽文", "stt_variant", ("gate",)),
    ("馬汶", "stt_variant", ("gate",)),
    ("marvy", "stt_variant", ("gate",)),
]

# Sentence-start only — too ambiguous mid-sentence，只有 detector 用
FAST_ONLY_WAKE_WORDS: list[str] = ["馬哥", "老馬", "杜比"]


def words_for(consumer: str) -> list[str]:
    """回傳指定 consumer（"detector" / "gate"）的喚醒詞清單，保留 entry 順序。"""
    return [word for word, _category, consumers in WAKE_WORD_ENTRIES if consumer in consumers]
