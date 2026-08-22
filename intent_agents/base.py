"""DeclarativeIntentAgent — Amazon Alexa-style declarative intent base.

每個 agent 用 declarative IntentSchema 列舉自己會接的意圖（regex pattern + slots +
confidence），bid() 由 base class 自動實作：

  gate(ctx)               — 早退條件（low wake_intent / empty query / hallucination）
                            回 reason str → dense Bid(0.0, reason=gate_reason)
  declare_intents()       — list[IntentSchema]，order = priority（first match wins）
  post_match_filter()     — schema 命中後的精細邏輯（regex 無法表達的，如 blocklist）
                            回 False → 繼續找下個 schema
  make_handler()          — schema + slots → coroutine（接到 controller 的實際 action）

Bid 永遠 dense：未命中 schema 也回 Bid(confidence=0.0, reason="no_match")，讓未來
70B verifier 看到「我看了，不是我」的明確 negative signal（5/19 Q3 verifier_replay 發現
2-agent 規模 bid vector 太稀就是這個問題）。

設計來源：5/19 設計討論，已記憶 `project_intent_bus.md`。

── 音素 fallback（喚醒+指令 AND-gate）─────────────────────────────────────────
regex 全部 miss 後，若 schema 有宣告 phonetic_keywords，會再試一次拼音 fuzzy：
喚醒（ctx.wake_intent 打中）**且**指令拼音（fuzzy ratio ≥ 門檻）都中才出價，任一沒
中就是 no_match——不會單靠音素就產生行為。這是 opt-in：schema 不填
phonetic_confidence 就完全不受影響。只該用在封閉、可窮舉的關鍵詞集合（skip/pause/
播放/查詢 這類動詞），開放式 slot schema（任意查詢字串）不該宣告，音素 fuzzy 對
自由文字誤判率太高（見 memory triadic_expert_pattern_domain_and_timing）。

比對演算法是「音節數對齊的滑動窗口」而非整串/partial ratio：keyword 轉拼音音節、
text 也轉每字拼音音節，取跟 keyword 等長的窗口比對取最高分——這是刻意選擇，
partial_ratio 對短詞（2-3 音節）太寬容，會把「換一首」「放一首」這種共用字尾但語意
相反的詞組混在一起（8/8 實測 partial_ratio=90.9，window ratio=83.3，仍需靠下方
literal-substring skip 加額外守門）。keyword 若整串已是 text 的字面 substring 一律
排除比對——代表 regex 已經試過、沒中是因為缺 marker/target 等結構條件，不是同音字
問題，音素 fallback 不該把它撿回來。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from intent_bus import Bid, IntentContext

try:
    from rapidfuzz import fuzz
    from pypinyin import lazy_pinyin
    from music_fastpath import _DEPS_OK as _PHONETIC_DEPS_OK
except ImportError:  # 同源 dep 缺 → phonetic fallback 靜默停用
    _PHONETIC_DEPS_OK = False


# 動詞窗口最多容忍幾個音節的句首前綴（「幫我」「請」這類）再開始比對。中文指令
# 動詞習慣在句首附近，不會埋在句子中段；深埋才比對到的通常是雜訊撞詞（8/8 實測：
# 不設界限時「馬文播放這個」誤配「放首歌」到 78.3 分，設界限後降到 71.4，門檻才有
# 意義——見模組 docstring「換一首」vs「放一首」案例，那個要靠 substring guard，
# 這個窗口界限是另一道獨立防線，擋的是不同種類的誤判）。
_PHONETIC_MAX_PREFIX_SYLLABLES = 2


def _phonetic_window_score(text: str, keyword: str) -> float:
    """text 對單一 keyword 的音節對齊窗口最高分（0-100）。見模組 docstring 選型理由。"""
    if not _PHONETIC_DEPS_OK or not text or not keyword or keyword in text:
        return 0.0
    kw_syllables = lazy_pinyin(keyword)
    text_syllables = lazy_pinyin(text)
    n = len(kw_syllables)
    if n == 0 or len(text_syllables) < n:
        return 0.0
    kw_joined = " ".join(kw_syllables).lower()
    max_start = min(_PHONETIC_MAX_PREFIX_SYLLABLES, len(text_syllables) - n)
    best = 0.0
    for i in range(max_start + 1):
        window = " ".join(text_syllables[i:i + n]).lower()
        best = max(best, fuzz.ratio(window, kw_joined))
    return best


def phonetic_best_match(text: str, keywords) -> float:
    """text 對 keywords 集合中任一詞的最佳音素相似分數（0-100）。跨 agent 共用
    （例如 MusicAgentV2 用它排除跟控制指令家族撞音的 case）。"""
    return max((_phonetic_window_score(text, kw) for kw in keywords), default=0.0)


@dataclass(frozen=True)
class IntentSchema:
    """Declarative intent: name + regex patterns + slot spec.

    patterns: list of regex strings; first hit wins. Named groups in pattern
              become slots (passed to reason_template + handler).
    required_slots: slot names that, if missing/empty, get added to missing_slots
                    (Alexa CanFulfillIntent 模式).
    reason_template: format string using slots dict + extra keys {matched, name}.
    """
    name: str
    confidence: float
    patterns: list[str]
    required_slots: list[str] = field(default_factory=list)
    reason_template: str = "{name}"
    # 音素 fallback（opt-in）：regex 全 miss 後才試。phonetic_confidence 是 None
    # 表示這個 schema 不參與音素 fallback（多數開放式 slot schema 應該維持 None——
    # 音素 fuzzy 只在封閉、可窮舉的關鍵詞集合上可靠，見 base.py 模組 docstring）。
    phonetic_keywords: list[str] = field(default_factory=list)
    phonetic_confidence: float | None = None


class DeclarativeIntentAgent:
    """Base for Amazon-style declarative intent agents."""

    name: str = "<override>"

    # Modes this agent participates in. Override in subclass — e.g.:
    #   MusicAgent:       {"normal", "stream"}    （遊戲中不該誤觸發音樂指令）
    #   Busted99Agent:    {"game"}               （只在遊戲模式吃 raw 語音）
    # 預設 {"normal"}：保守，subclass 必須顯式宣告才能在其他模式出價。
    mode_compatible: set[str] = frozenset({"normal"})

    # 音素 fallback 門檻。wake_intent is None（Track A 精確喚醒）或 >= 這個值才算
    # 「喚醒打中」；對齊既有 agent 的 LOW_WAKE_THRESHOLD 慣例（music_agent 0.65）。
    PHONETIC_WAKE_MIN_INTENT: float = 0.65
    # 拼音 fuzzy ratio 門檻，跟 command_fastpath 同門檻（DEFAULT_THRESHOLD=85）。
    PHONETIC_FUZZ_THRESHOLD: float = 85.0

    # ── Subclass hooks ────────────────────────────────────────────────────────

    def declare_intents(self) -> list[IntentSchema]:
        raise NotImplementedError("subclass must implement declare_intents()")

    def gate(self, ctx: IntentContext) -> str | None:
        """Subclass-overridable gate. Return reason string to short-circuit
        with dense 0.0 bid; None to proceed.

        NOTE: mode compatibility is checked separately by `bid()` before this
        runs, so subclass overrides do NOT need to call super().gate().
        """
        return None

    def post_match_filter(
        self, schema: IntentSchema, slots: dict[str, str], ctx: IntentContext
    ) -> bool:
        """Optional fine-grained filter after regex match.

        Return False to reject this schema match and continue to next schema.
        Default: accept all.
        """
        return True

    def make_handler(
        self, schema: IntentSchema, slots: dict[str, str], ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        """Build coroutine to run when this bid wins. Default = noop."""
        return self._noop

    # ── Auto-implemented bid() ────────────────────────────────────────────────

    def bid(self, ctx: IntentContext) -> Bid:
        # Pre-gate: mode compatibility (non-overridable; subclass can't bypass)
        if ctx.mode not in self.mode_compatible:
            return self._dense_zero(f"mode_mismatch:{ctx.mode}")

        # Subclass-overridable gate
        gate_reason = self.gate(ctx)
        if gate_reason is not None:
            return self._dense_zero(gate_reason)

        text = (ctx.query or "").strip()
        if not text:
            return self._dense_zero("empty_query")

        for schema in self.declare_intents():
            for pat in schema.patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if not m:
                    continue
                slots = {k: (v or "") for k, v in m.groupdict().items()}
                if not self.post_match_filter(schema, slots, ctx):
                    continue
                missing = [s for s in schema.required_slots
                           if not slots.get(s, "").strip()]
                fmt_kwargs = {**slots, "matched": m.group(0), "name": schema.name}
                try:
                    reason = schema.reason_template.format(**fmt_kwargs)
                except (KeyError, IndexError):
                    reason = schema.name
                return Bid(
                    name=self.name,
                    confidence=schema.confidence,
                    handler=self.make_handler(schema, slots, ctx),
                    reason=reason,
                    missing_slots=missing,
                )

        phonetic_bid = self._try_phonetic_bid(text, ctx)
        if phonetic_bid is not None:
            return phonetic_bid

        return self._dense_zero("no_match")

    def resolve_intent(
        self, intent_name: str, slots: dict[str, str], ctx: IntentContext
    ) -> Bid | None:
        """Audio rescue 專用：LLM 已經指名要哪個 intent（跳過 regex 文字比對），
        這裡只重放 bid() 除了「regex 找 schema」以外的所有既有守門邏輯——
        mode_compatible / gate() / post_match_filter() / make_handler()——確保
        音訊路徑跟正常 bid() 路徑受同一套系統狀態檢查（例如 VolumeAgent 沒開
        播放不該真的調音量）。

        v1 刻意不處理 missing slots（不接 resolver/追問流程）：required_slots
        沒填滿 → 視為 no-match 回 None，交由 caller 走純聊天 fallback，避免
        audio rescue 疊加 vector-intent resolve 的複雜度。
        """
        if ctx.mode not in self.mode_compatible:
            return None
        if self.gate(ctx) is not None:
            return None

        for schema in self.declare_intents():
            if schema.name != intent_name:
                continue
            filled = {k: (v or "") for k, v in (slots or {}).items()}
            if not self.post_match_filter(schema, filled, ctx):
                return None
            missing = [s for s in schema.required_slots if not filled.get(s, "").strip()]
            if missing:
                return None
            fmt_kwargs = {**filled, "matched": ctx.query or "", "name": schema.name}
            try:
                reason = schema.reason_template.format(**fmt_kwargs)
            except (KeyError, IndexError):
                reason = schema.name
            return Bid(
                name=self.name,
                confidence=schema.confidence,
                handler=self.make_handler(schema, filled, ctx),
                reason=f"audio_rescue:{reason}",
                missing_slots=[],
            )
        return None

    # ── Internals ─────────────────────────────────────────────────────────────

    def _try_phonetic_bid(self, text: str, ctx: IntentContext) -> Bid | None:
        """喚醒+指令音素 AND-gate。兩個訊號都要打中才出價，見模組 docstring。"""
        if not _PHONETIC_DEPS_OK:
            return None
        wake_hit = ctx.wake_intent is None or ctx.wake_intent >= self.PHONETIC_WAKE_MIN_INTENT
        if not wake_hit:
            return None

        for schema in self.declare_intents():
            if not schema.phonetic_keywords or schema.phonetic_confidence is None:
                continue
            score = phonetic_best_match(text, schema.phonetic_keywords)
            if score < self.PHONETIC_FUZZ_THRESHOLD:
                continue
            slots: dict[str, str] = {}
            if not self.post_match_filter(schema, slots, ctx):
                continue
            missing = [s for s in schema.required_slots if not slots.get(s, "").strip()]
            fmt_kwargs = {**slots, "matched": text, "name": schema.name}
            try:
                reason = schema.reason_template.format(**fmt_kwargs)
            except (KeyError, IndexError):
                reason = schema.name
            return Bid(
                name=self.name,
                confidence=schema.phonetic_confidence,
                handler=self.make_handler(schema, slots, ctx),
                reason=f"phonetic:{reason}:{score:.0f}",
                missing_slots=missing,
            )
        return None

    def _dense_zero(self, reason: str) -> Bid:
        return Bid(name=self.name, confidence=0.0, handler=self._noop, reason=reason)

    async def _noop(self) -> None:
        pass
