"""CooldownAwarePool — 把數個 free-tier LLM endpoint 當成一個算力池。

問題（2026-05-21 prod，單人測試就撞 Groq 500K TPD 上限）：每個 caller 各自寫
try/except + retry，429 後**每次呼叫還是先撞同一個 endpoint** 才 fallback，浪費
round-trip + 拖慢 pipeline。stt_cleaner 已有 per-engine cooldown（proven pattern），
但 gemini_router_llm 完全沒 cooldown 記憶（2026-05-20 一晚 43 次 Groq 429）。

解法（project_llm_tier_wrapper.md，Jack 拍板）：抽成共用 pool。
- 429 → mark_429 記 cooldown_until，冷卻期 next_available **直接跳過不撞**
- TPM 接近上限（budget*headroom）也跳，留 buffer
- next_available() 是唯一入口——caller 不再自己寫 try/except chain，pool 自動分流
  到下一個有 quota 的 endpoint；全滿回 None（caller 兜底）

本模組只管**狀態 + 選 endpoint**（純邏輯、可單測）；實際 LLM 呼叫由 caller 拿
回傳的 endpoint 自己做，做完用 record_usage / mark_429 回報。
"""
from __future__ import annotations

import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_RETRY_AFTER_DEFAULT = 30.0


def parse_retry_after(err: str, default: float = _RETRY_AFTER_DEFAULT) -> float:
    """從 429 訊息抽 retry-after 秒數。

    支援 Groq 形式 'Please try again in 4m18.8544s' / 'in 30s' / 'in 1m'。
    抽不到 → default（保守冷卻，不要 0 否則等於沒冷卻）。
    """
    if not err:
        return default
    m = re.search(r"try again in\s+(?:(\d+)\s*m)?\s*(?:([\d.]+)\s*s)?", err, re.IGNORECASE)
    if m:
        mins = float(m.group(1)) if m.group(1) else 0.0
        secs = float(m.group(2)) if m.group(2) else 0.0
        total = mins * 60 + secs
        if total > 0:
            return total
    # 退而求其次：'retry_after: X' 之類
    m2 = re.search(r"retry[_-]?after['\":\s]+([\d.]+)", err, re.IGNORECASE)
    if m2:
        return float(m2.group(1))
    return default


@dataclass
class PoolEndpoint:
    """池中的一個 endpoint。client/model 給 caller 拿去呼叫；其餘是 pool 的狀態。"""
    name: str
    client: Any = None
    model: str = ""
    tpm_budget: int = 6000
    cooldown_until: float = 0.0
    usage_window: deque = field(default_factory=deque)  # (ts, tokens) 滾動 60s


class CooldownAwarePool:
    TPM_HEADROOM = 0.75      # 用量 > budget*headroom 就跳，留 buffer 不撞線
    USAGE_WINDOW_S = 60.0

    def __init__(self, endpoints: list[PoolEndpoint], *,
                 clock: Callable[[], float] = time.time):
        self.endpoints = list(endpoints)   # 註冊順序 = 優先序
        self._clock = clock

    def current_tpm(self, ep: PoolEndpoint) -> int:
        """滾動 60s 視窗內的 token 用量（順便清掉過期項）。"""
        cutoff = self._clock() - self.USAGE_WINDOW_S
        while ep.usage_window and ep.usage_window[0][0] < cutoff:
            ep.usage_window.popleft()
        return sum(tok for _, tok in ep.usage_window)

    def next_available(self) -> Optional[PoolEndpoint]:
        """按優先序回第一個沒在冷卻、TPM 未近上限的 endpoint；全滿回 None。"""
        now = self._clock()
        for ep in self.endpoints:
            if now < ep.cooldown_until:
                continue
            if self.current_tpm(ep) > ep.tpm_budget * self.TPM_HEADROOM:
                continue
            return ep
        return None

    def mark_429(self, ep: PoolEndpoint, err_str: str = "", *,
                 retry_after: Optional[float] = None) -> None:
        """endpoint 被 429 → 設冷卻到 now + retry_after（從錯誤訊息解析或顯式給）。"""
        secs = retry_after if retry_after is not None else parse_retry_after(err_str)
        ep.cooldown_until = self._clock() + secs

    def record_usage(self, ep: PoolEndpoint, tokens: int) -> None:
        """呼叫成功後回報用量，進滾動 TPM 視窗。"""
        ep.usage_window.append((self._clock(), max(0, int(tokens))))


# ── Dispatch loop（pool + 呼叫 = router）──────────────────────────────────────

_RATE_LIMIT_HINTS = ("429", "rate limit", "rate_limit", "ratelimit",
                     "quota", "tokens per day", "tpd", "too many requests")
_TRANSIENT_COOLDOWN = 5.0   # 非 429 暫時性錯誤（timeout/5xx）的短冷卻，避免立刻重撞


def is_rate_limit(exc: BaseException) -> bool:
    """粗略判斷例外是不是 rate-limit（決定冷卻時間從訊息解析 vs 用短預設）。"""
    s = str(exc).lower()
    return any(h in s for h in _RATE_LIMIT_HINTS)


async def dispatch(pool: CooldownAwarePool, call_fn) -> Optional[Any]:
    """池子（狀態）+ call_fn（I/O）= router。

    一直問 pool.next_available()，對回傳的 endpoint 跑 call_fn(ep)：
      - 成功：call_fn 回 (result, tokens) → record_usage → 回 result
      - rate-limit：mark_429（從訊息解析冷卻）→ 換下一個
      - 其他暫時性錯誤：短冷卻 → 換下一個
    全部冷卻/近上限（next_available 回 None）→ 回 None，caller 兜底。

    每次失敗都會把該 endpoint 冷卻 → next_available 必定收斂，不會無限迴圈。
    call_fn(ep) 必須回 (result, tokens) 或 raise。
    """
    while (ep := pool.next_available()) is not None:
        try:
            result, tokens = await call_fn(ep)
        except Exception as e:
            if is_rate_limit(e):
                pool.mark_429(ep, str(e))
            else:
                pool.mark_429(ep, retry_after=_TRANSIENT_COOLDOWN)
            continue
        pool.record_usage(ep, tokens)
        return result
    return None


# ── 三層門面（caller 選 tier，tier 內由 pool 選 endpoint）─────────────────────

class TieredLLMRouter:
    """quick（輕量）/ analyze（重值）各一個 CooldownAwarePool；paid（付費）特例。

    caller 強制必填（per-agent 用量歸屬）。quick/analyze 假設 endpoint 皆 OpenAI 相容
    （Groq/Cerebras/SambaNova/Together/Fireworks/OpenRouter 都是），統一 call_fn。
    """

    def __init__(self, quick_pool: CooldownAwarePool, analyze_pool: CooldownAwarePool):
        self.quick_pool = quick_pool
        self.analyze_pool = analyze_pool
        # per-caller token 歸屬：「誰最會吃 token」（記憶 project_llm_tier_wrapper 的目標）
        self.usage_by_caller: Counter = Counter()

    async def quick(self, prompt: str, *, caller: str, system: Optional[str] = None,
                    max_tokens: int = 200, temperature: float = 0.7,
                    json: bool = False) -> Optional[str]:
        return await self._chat(self.quick_pool, prompt, system, max_tokens, temperature, json, caller)

    async def analyze(self, prompt: str, *, caller: str, system: Optional[str] = None,
                      max_tokens: int = 300, temperature: float = 0.3,
                      json: bool = False) -> Optional[str]:
        return await self._chat(self.analyze_pool, prompt, system, max_tokens, temperature, json, caller)

    async def _chat(self, pool, prompt, system, max_tokens, temperature, json, caller) -> Optional[str]:
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]

        async def _call(ep: PoolEndpoint):
            kwargs = dict(model=ep.model, messages=messages, max_tokens=max_tokens,
                          temperature=temperature, stream=False)
            if json:
                kwargs["response_format"] = {"type": "json_object"}
            resp = await ep.client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content
            usage = getattr(resp, "usage", None)
            tokens = usage.total_tokens if usage else 0
            self.usage_by_caller[caller] += tokens   # per-agent 歸屬
            return content, tokens

        return await dispatch(pool, _call)
