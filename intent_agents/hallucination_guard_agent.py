"""
HallucinationGuardAgent — 主動 bid 高分把 STT 幻覺 wake 吞掉。

5/18 audit：24 wake / 8 是「bus no_bids 但 LLM 用 conv_buffer 編 plausible
答案」噪音回應。讓 guard agent 主動出價 0.96 壓過 music/nemoclaw，handler
silent swallow — bus 行為不變，零 controller 改動。

偵測 heuristics（任一命中即出價，全是「保守 high-precision」設計）：

1. **wake-word loop** (conf 0.96)：raw 含 ≥3 個 wake 詞
   出現（"Hi Marvin Hi Marvin Hi Marvin..."）

2. **exotic-script presence** (conf 0.92)：raw 含 Hangul / 西里爾 /
   越南文 accent / 日文假名 — bot 場景不該出現這些，幾乎必為 STT 雜訊

3. **tiny stripped query** (conf 0.96)：strip wake + 標點後 < 3 字
   有效內容（"Hi Marvin!" 之類超短 wake fragment）

4. **Track B no-wake-word + 短 query** (conf 0.92)：wake_intent 非 None
   且 raw 完全沒「馬文/Marvin」且 query ≤ 13 字。5/18 audit 切點：
   bad case「3F D呀每天都去點」(10)、「嫂嫂有成功啊成功率越高」(12)、
   「幹打開小女兒哭」(7) 都 ≤13；good case「人格壽格壽司...」(22)、
   「化膿是四個大跟...」(18) ≥18 不會誤殺

刻意不抓：raw 含「馬文/Marvin」但 query 跟 raw 脫節（#7 #13 #15）：
沒可靠 sync 訊號判斷，列 P2 待解
"""
from __future__ import annotations

import logging
import re

from intent_bus import Bid, IntentContext

logger = logging.getLogger("cogs.voice_controller.guard")

# 跟 wake_detector 一致的喚醒詞 pattern（保守 set；不抓 STT 變體）
_WAKE_PATTERNS = (
    "馬文", "marvin", "marvy", "麻文", "媽文", "瑪文",
)
_WAKE_RE = re.compile("|".join(_WAKE_PATTERNS), re.IGNORECASE)


def _count_wake_occurrences(text: str) -> int:
    return len(_WAKE_RE.findall(text or ""))


def _has_wake_word(text: str) -> bool:
    return _WAKE_RE.search(text or "") is not None


def _has_exotic_script(text: str) -> bool:
    """raw 含 Hangul / 西里爾 / 越南文 accent / 日文假名 → 必為 STT 雜訊。

    Marvin 場景 user 講中英文，這些字根本不該出現。任一字符出現即判幻覺。
    """
    if not text:
        return False
    for c in text:
        cp = ord(c)
        if 0xAC00 <= cp <= 0xD7AF:    # Hangul Syllables
            return True
        if 0x0400 <= cp <= 0x04FF:    # Cyrillic
            return True
        if 0x1E00 <= cp <= 0x1EFF:    # Latin Extended Additional (含越南文)
            return True
        if 0x3040 <= cp <= 0x30FF:    # Hiragana + Katakana
            return True
    return False


async def _swallow():
    """guard winner 的 handler — 不做任何副作用，純 log。"""
    # 不發 TTS / LLM / channel msg；log 留下 trace 供 audit
    return None


class HallucinationGuardAgent:
    name = "guard"
    # 幻覺啟發式針對「喚醒情境」設計；遊戲模式吃 raw 短答案（「50」「21」），
    # 跑這些規則會誤吞有效答案 → gate 掉 game。
    mode_compatible = frozenset({"normal", "stream"})

    # bid 等級
    CONF_HIGH = 0.96   # 明確幻覺，壓過 music 0.95 / nemoclaw 0.95
    CONF_MID = 0.92    # 疑似幻覺（multi-script，但 music 強訊號可 override 不到）

    def __init__(self, controller):
        self.ctrl = controller  # 留著未來可 log 進 stt_logger

    def bid(self, ctx: IntentContext) -> Bid | None:
        if ctx.mode not in self.mode_compatible:
            return None  # 遊戲模式不攔截，讓 game agent 接 raw 答案
        raw = ctx.original_raw or ctx.raw_text or ""
        query = ctx.query or ""

        # 1. wake-word loop（≥3 個 wake 出現算 STT loop）
        wake_count = _count_wake_occurrences(raw)
        if wake_count >= 3:
            return self._mk_bid(self.CONF_HIGH, f"wake_loop:{wake_count}x")

        # 2. exotic-script presence（Hangul/Cyrillic/越南/日假名）
        if _has_exotic_script(raw):
            return self._mk_bid(self.CONF_MID, "exotic_script")

        # 3. Track B 無 wake + 短 query（切點 13 字依 5/18 audit）
        #    但若 query 含 music kw（"播放"/"放音樂"...），讓 music agent 接，
        #    避免誤殺「麻煩播放幹大事」(no marvin, wake=1.0, len=7) 之類有效點歌
        if (ctx.wake_intent is not None
                and not _has_wake_word(raw)
                and len(query) <= 13
                and not self._has_play_keyword(query)):
            return self._mk_bid(
                self.CONF_MID,
                f"track_b_no_wake_short:len={len(query)} wake={ctx.wake_intent}",
            )

        # 4. tiny stripped query（"Hi Marvin!" 之類超短 wake fragment）
        stripped = _WAKE_RE.sub("", query).strip("，,、！!？? ")
        if len(stripped) < 3:
            raw_stripped = _WAKE_RE.sub("", raw).strip("，,、！!？? Hihi")
            if len(raw_stripped) < 3:
                return self._mk_bid(self.CONF_HIGH, f"empty_after_strip:'{stripped}'")

        return None

    def _has_play_keyword(self, query: str) -> bool:
        """檢查 query 是否含 music play kw。引用 controller 的 kw 列表避免 drift。"""
        q = (query or "").lower()
        kws = getattr(self.ctrl, "_STRONG_PLAY_KW", []) + \
              getattr(self.ctrl, "_WEAK_PLAY_KW", [])
        return any(kw.lower() in q for kw in kws)

    def _mk_bid(self, confidence: float, reason: str) -> Bid:
        return Bid(
            name=self.name,
            confidence=confidence,
            handler=_swallow,
            reason=reason,
        )
