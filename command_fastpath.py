"""控制指令 fast-path：糊字/同音字控制指令 → 正規指令（skip/pause/resume/stop）。

延伸 MusicFastPath 的拼音 fuzzy 到「控制指令」域。STT 常把控制詞糊成同音字
（下一首→下一手、切歌→切鴿、暫停→暫聽），精確關鍵字表 miss → 掉 cleaner LLM。
toneless 拼音讓同音字塌成同一字串 → fuzzy 救回，直送 IntentBus 跳過 2.5s cleaner。

兩個出口：
- match_command_action(text) → "skip"/"pause"/"resume"/"stop" 或 None（給偵測器當 fuzzy 兜底）
- normalize_command(text)    → 正規指令字串（下一首…）或 None（給 confirmation_flow 改寫，
  讓下游 PlaybackControlAgent/MusicAgentV2 的 regex pattern 命中）

守門（防閒聊誤觸，對齊 is_short_skip_command 精神）：剝允許前綴（馬文/快…）後，剩餘須
短（≤_MAX_LEN）且整串 fuzz.ratio≥門檻——指令要「是」而非「含」，問句/否定/長句的多餘
詞會把 whole-string ratio 稀釋到門檻下，自然被擋（無需另寫位置/否定名單）。

優雅降級：rapidfuzz/pypinyin 缺 → 一律回 None（feature 自動關閉，不 crash）。
"""
from __future__ import annotations

import os
import re

from intent_agents.constants import (
    MUSIC_SKIP_KW, MUSIC_PAUSE_KW, MUSIC_RESUME_KW, MUSIC_STOP_KW,
)
from music_fastpath import _DEPS_OK, to_pinyin

try:
    from rapidfuzz import fuzz, process
except ImportError:  # 與 music_fastpath 同源 dep；缺 → _DEPS_OK 已 False
    pass

DEFAULT_THRESHOLD = 85.0
_MAX_LEN = 8   # 剝前綴後剩餘字數上限；控制指令都很短（下一首/停止播放≤4），長→非裸命令

# kill-switch：晚間真人流量若糊字控制誤觸，設 MARVIN_COMMAND_FASTPATH=0 + 重啟即整個停用。
# 預設 ON（feature 保守：只在精確表 miss + ratio≥門檻才觸發，blast radius 小）。
_ENABLED = os.getenv("MARVIN_COMMAND_FASTPATH", "1") == "1"

# 命中後回給下游的正規指令：都在 PlaybackControlAgent/MusicAgentV2 的 0.85+ pattern 內
_CANONICAL = {"skip": "下一首", "pause": "暫停音樂", "resume": "繼續播", "stop": "停止播放"}

# 允許的命令導引前綴（address/intensifier/soft connector），對齊 skip_intent._ALLOWED_PREFIX_RE。
# **不含**否定/疑問/推論詞（不要/為什麼/應該）——那些留在字串裡稀釋 ratio 才擋得住閒聊。
# 也不含 play 動詞（播/放）——那是點歌訊號、不該替控制指令剝掉。
_PREFIX_RE = re.compile(r"^\s*(?:馬文|欸|喂|快|現在|給我|拜託|請|那|我要)?[\s,，。、]*")

_HAN_RE = re.compile(r"[一-鿿]")


def _build_choices() -> tuple[dict[int, str], dict[int, str]]:
    """把各 action 的中文關鍵詞轉 toneless 拼音，建 idx→pinyin / idx→action 兩表。"""
    idx_pinyin: dict[int, str] = {}
    idx_action: dict[int, str] = {}
    families = [
        ("skip", MUSIC_SKIP_KW), ("pause", MUSIC_PAUSE_KW),
        ("resume", MUSIC_RESUME_KW), ("stop", MUSIC_STOP_KW),
    ]
    for action, kws in families:
        for kw in kws:
            if not _HAN_RE.search(kw):   # 純英文（skip/pause…）由精確表處理，不進拼音池
                continue
            py = to_pinyin(kw)
            if not py:
                continue
            i = len(idx_pinyin)
            idx_pinyin[i] = py
            idx_action[i] = action
    return idx_pinyin, idx_action


_IDX_PINYIN, _IDX_ACTION = _build_choices() if _DEPS_OK else ({}, {})


def _match(text: str) -> tuple[str, str, float] | None:
    """(action, canonical, score) 若糊字命中某控制指令，否則 None。"""
    if not _ENABLED or not _DEPS_OK or not _IDX_PINYIN or not text or not text.strip():
        return None
    stripped = _PREFIX_RE.sub("", text.strip()).strip()
    if not stripped or len(stripped) > _MAX_LEN:
        return None
    qpy = to_pinyin(stripped)
    if not qpy:
        return None
    res = process.extractOne(qpy, _IDX_PINYIN, scorer=fuzz.ratio)
    if res is None:
        return None
    _pinyin_val, score, idx = res
    if score < DEFAULT_THRESHOLD:
        return None
    action = _IDX_ACTION[idx]
    return action, _CANONICAL[action], float(score)


def match_command_action(text: str) -> str | None:
    """糊字控制指令 → "skip"/"pause"/"resume"/"stop"，否則 None。"""
    hit = _match(text)
    return hit[0] if hit else None


def normalize_command(text: str) -> str | None:
    """糊字控制指令 → 正規指令字串（下一首/暫停音樂…），否則 None。"""
    hit = _match(text)
    return hit[1] if hit else None
