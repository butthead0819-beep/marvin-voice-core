"""
MusicAgent — 對 wake 後的音樂播放/控制意圖出價。

Confidence 規約：
  0.95 — 控制詞 (skip/stop/pause/resume) 或 強訊號 play
  0.80 — 弱訊號 play + music marker
  0.55 — 弱訊號 play + 後續長字串但無 marker（保留 fallback search 機會）
  None — 無命中、UI 詞 blocklist、low confidence wake

Keyword 列表跟 controller._STRONG_PLAY_KW 等保持同步（Phase 1 直接讀
controller class-level constants，未來抽到共用 module）。
"""
from __future__ import annotations

from collections import Counter

from intent_bus import Bid, IntentContext


# 弱訊號 play 需要的二次驗證標記
_MUSIC_INTENT_MARKERS = ("的", "歌", "曲", "音樂", "mv", "ost", "歌詞", "歌手",
                         "一首", "那首", "這首")
_NON_MUSIC_TARGETS = frozenset([
    # UI / 系統詞（原 blocklist）
    "控制", "清單", "列表", "設定", "選項",
    "畫面", "頁面", "音量", "狀態",
    # Demonstrative pronouns + 常見口語通用詞
    # 「播放這個」「播放那個」「幫我找東西」「播放什麼」這類對話脈絡
    # 不該被當成點歌（誤接 0.55 weak_play_only）
    "這個", "那個", "它", "他", "她", "東西", "什麼",
])

# 重複幻覺偵測：3+ 字 substring 重複 ≥3 次視為 STT loop hallucination
_REPETITION_WINDOW = 3       # substring 長度
_REPETITION_THRESHOLD = 3    # 出現次數閾值
_REPETITION_MIN_LEN = 6      # query 長度下限（短 query 跳過避免誤殺）
_REPETITION_MAX_LEN = 200    # query 過長（>200 字）跳過，重複可能是長串歌名


class MusicAgent:
    name = "music"
    # 0.65 對齊 LLM veto 閾值：wake_intent < 0.65 已被 wake_detector LLM veto
    # 強制刷掉，能跑到 agent.bid 的就是「LLM 已認可的 wake」。MusicAgent 額外
    # 帶 kw + marker + UI blocklist + repetition guard 多層防誤觸發，
    # 不需要再做 0.80 二次保守 gate（5/18 18:16 incident：wake_intent=0.7
    # 被 0.80 擋掉、fall through 到 Marvin LLM 假承諾「已為你播放」但音樂
    # 沒播）。Legacy fast-tracks 在 voice_controller.py 仍維持 0.80 保守。
    LOW_WAKE_THRESHOLD = 0.65

    def __init__(self, controller):
        self.ctrl = controller

    @staticmethod
    def _looks_repetitive(query: str) -> bool:
        """STT loop hallucination 偵測：任一 3+ 字 substring 在 query 中
        出現 ≥3 次視為重複幻覺。

        例：「播放陶喆 陶喆 陶喆 陶喆」「馬文播放,馬文播放,馬文播放,馬文播放」。
        過短 query (<6 字) 跳過避免誤殺；過長 (>200 字) 跳過因可能是長串歌名。
        """
        q_len = len(query)
        if q_len < _REPETITION_MIN_LEN or q_len > _REPETITION_MAX_LEN:
            return False
        windows = [query[i:i + _REPETITION_WINDOW]
                   for i in range(q_len - _REPETITION_WINDOW + 1)]
        if not windows:
            return False
        top_count = Counter(windows).most_common(1)[0][1]
        return top_count >= _REPETITION_THRESHOLD

    def bid(self, ctx: IntentContext) -> Bid | None:
        # low-confidence wake → 不出價（避免副作用誤觸發）
        if ctx.wake_intent is not None and ctx.wake_intent < self.LOW_WAKE_THRESHOLD:
            return None

        q = ctx.query.lower()
        if not q:
            return None

        # STT loop hallucination guard — 重複字段直接退出
        if self._looks_repetitive(ctx.query):
            return None

        # 1. 控制詞優先（PAUSE/RESUME 早於 STOP 避免 substring 撞車）
        for kw in self.ctrl._MUSIC_SKIP_KW:
            if kw in q:
                return self._control_bid(ctx, "skip")
        for kw in self.ctrl._MUSIC_PAUSE_KW:
            if kw in q:
                return self._control_bid(ctx, "pause")
        for kw in self.ctrl._MUSIC_RESUME_KW:
            if kw in q:
                return self._control_bid(ctx, "resume")
        for kw in self.ctrl._MUSIC_STOP_KW:
            if kw in q:
                return self._control_bid(ctx, "stop")

        # 2. 強訊號 play
        for kw in self.ctrl._STRONG_PLAY_KW:
            if kw in q:
                return self._play_bid(ctx, 0.95, f"strong_play:{kw}")

        # 3. 弱訊號 play — 需要二次驗證
        for kw in self.ctrl._WEAK_PLAY_KW:
            if kw not in q:
                continue
            # 有 music marker → 0.80
            if any(m in q for m in _MUSIC_INTENT_MARKERS):
                return self._play_bid(ctx, 0.80, f"weak_play+marker:{kw}")
            # 無 marker：檢查弱訊號詞後續內容
            parts = q.split(kw, 1)
            if len(parts) < 2:
                return None
            after = parts[1].strip("，,、！!？?。. ")
            if len(after) < 2:
                return None
            if after in _NON_MUSIC_TARGETS:
                return None  # UI 詞 blocklist
            # 長字串 + 非 UI 詞 → 弱訊號 fallback
            return self._play_bid(ctx, 0.55, f"weak_play_only:{kw}->{after[:20]}")

        return None

    # ── handler builders ──────────────────────────────────────────────────

    def _control_bid(self, ctx: IntentContext, cmd: str) -> Bid:
        return Bid(
            name=self.name,
            confidence=0.95,
            handler=lambda: self.ctrl._safe_music_command(ctx.speaker, ctx.query, cmd),
            reason=f"control:{cmd}",
        )

    def _play_bid(self, ctx: IntentContext, confidence: float, reason: str) -> Bid:
        return Bid(
            name=self.name,
            confidence=confidence,
            handler=lambda: self.ctrl._safe_music_command(ctx.speaker, ctx.query, "play"),
            reason=reason,
        )
