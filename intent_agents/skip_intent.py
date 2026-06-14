"""Skip-intent predicate — pure 判定「query 是否真的是 skip 指令」。

2026-05-26 bug：IBA-T0 + music_agent_v2 control_skip 用單純 substring 匹配，
導致閒聊提到「下一首」就誤觸發 skip（「為什麼你下一首」「不喜歡下一首歌」等）。

規則（per tests/test_skip_intent_predicate.py）：
  1. 空字串 / 全空白 → False
  2. 長句（≥20 字）一律拒絕，幾乎不可能是純命令
  3. 關鍵字必須在「句首區」出現（容許 address「馬文」或 intensifier「快」等小前綴）
  4. 關鍵字前有否定（「不要」）/ 疑問（「為什麼」）/ 推論（「應該」「沒有」）等
     → 不在 allowed prefix 名單內 → 視為閒聊提及而非命令

Pure：純字串判定，無 IO / 無 LLM。提供給 IBA-T0 跟 music_agent_v2 共用，
避免兩條 path 各寫各的判定漂移。
"""
from __future__ import annotations

import re
from typing import Iterable

# 句首容許的「命令導引前綴」：address（馬文/欸/喂）、intensifier（快/現在/拜託）、
# soft connector（給我/請/那）、意願（我要）。**不含**否定/疑問/推論詞（不要/為什麼/應該等）。
# 「我要」是自然命令引導（「我要切歌」）；「我不要」「我要不要」因關鍵字落在前綴之後仍正確拒絕。
_ALLOWED_PREFIX_RE = re.compile(
    r"^\s*(?:馬文|欸|喂|快|現在|給我|拜託|請|那|我要)?[\s,，。、]*"
)

_LONG_SENTENCE_CHARS = 20


def is_short_skip_command(text: str, keywords: Iterable[str]) -> bool:
    """text 是否為一個 skip 命令（不只是閒聊提到關鍵字）。

    keywords：要比對的關鍵字集合（如 MUSIC_DIRECT_SKIP_KW）。
    """
    t = (text or "").strip()
    if not t:
        return False
    if len(t) >= _LONG_SENTENCE_CHARS:
        return False

    # 找最早出現的關鍵字位置 + 同 kw 出現次數（給 emphasis 豁免用）
    earliest = -1
    max_kw_count = 0
    for kw in keywords:
        if not kw:
            continue
        idx = t.find(kw)
        if idx >= 0 and (earliest < 0 or idx < earliest):
            earliest = idx
        # 同 kw 連續出現次數（emphasis pattern「下一首下一首」「Siri下一首下一首」）
        if kw and t.count(kw) > max_kw_count:
            max_kw_count = t.count(kw)
    if earliest < 0:
        return False

    # 規則 a：同 kw 連講 ≥2 次 = 強調命令，豁免位置檢查
    # 抓「Siri下一首下一首」「Hey下一首下一首」等雙語 address 場景，
    # 避免維護無止盡的 address allowlist
    if max_kw_count >= 2:
        return True

    # 規則 b：句首容許前綴的結束位置；關鍵字必須在這之前/之內出現
    m = _ALLOWED_PREFIX_RE.match(t)
    prefix_end = m.end() if m else 0
    return earliest <= prefix_end
