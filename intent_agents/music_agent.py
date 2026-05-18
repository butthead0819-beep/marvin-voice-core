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

from intent_bus import Bid, IntentContext


# 弱訊號 play 需要的二次驗證標記
_MUSIC_INTENT_MARKERS = ("的", "歌", "曲", "音樂", "mv", "ost", "歌詞", "歌手",
                         "一首", "那首", "這首")
_NON_MUSIC_TARGETS = frozenset(["控制", "清單", "列表", "設定", "選項",
                                 "畫面", "頁面", "音量", "狀態"])


class MusicAgent:
    name = "music"
    LOW_WAKE_THRESHOLD = 0.80

    def __init__(self, controller):
        self.ctrl = controller

    def bid(self, ctx: IntentContext) -> Bid | None:
        # low-confidence wake → 不出價（避免副作用誤觸發）
        if ctx.wake_intent is not None and ctx.wake_intent < self.LOW_WAKE_THRESHOLD:
            return None

        q = ctx.query.lower()
        if not q:
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
            handler=lambda: self.ctrl._handle_voice_music_command(ctx.speaker, ctx.query, cmd),
            reason=f"control:{cmd}",
        )

    def _play_bid(self, ctx: IntentContext, confidence: float, reason: str) -> Bid:
        return Bid(
            name=self.name,
            confidence=confidence,
            handler=lambda: self.ctrl._handle_voice_music_command(ctx.speaker, ctx.query, "play"),
            reason=reason,
        )
