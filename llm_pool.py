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

import os
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

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
    # ① Daily budget（6/2）：per-minute TPM 看不到當日 token cap（TPD），導致 provider
    # 每分鐘看似閒、其實當日快爆 → 一直被選 → 撞 TPD 429。daily_budget=0 表未知/無限制
    # （不罰）。只有已知 daily cap 的 provider（如 Groq TPD 50萬）才填，誠實不亂估。
    daily_budget: int = 0
    daily_used: int = 0
    daily_reset_at: float = 0.0   # 滾動 24h 視窗到期時間（first use 時設 now+86400）


class CooldownAwarePool:
    TPM_HEADROOM = 0.75      # 用量 > budget*headroom 就跳，留 buffer 不撞線
    DAILY_HEADROOM = 0.92    # daily 用量 > budget*headroom 就跳（比 TPM 寬，daily 粗粒度）
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

    def daily_ratio(self, ep: PoolEndpoint) -> float:
        """① 當日 token 用量佔 daily_budget 比例。budget=0（未知）→ 0.0（不罰）。
        滾動 24h 視窗過期 → 視為重置回 0.0。"""
        if ep.daily_budget <= 0:
            return 0.0
        if self._clock() >= ep.daily_reset_at:
            return 0.0  # 視窗到期，當日歸零
        return ep.daily_used / ep.daily_budget

    def next_available(self) -> Optional[PoolEndpoint]:
        """按優先序回第一個沒在冷卻、TPM 與 daily 都未近上限的 endpoint；全滿回 None。"""
        now = self._clock()
        for ep in self.endpoints:
            if now < ep.cooldown_until:
                continue
            if self.current_tpm(ep) > ep.tpm_budget * self.TPM_HEADROOM:
                continue
            if self.daily_ratio(ep) > self.DAILY_HEADROOM:  # ① daily 近上限也跳
                continue
            return ep
        return None

    def mark_429(self, ep: PoolEndpoint, err_str: str = "", *,
                 retry_after: Optional[float] = None) -> None:
        """endpoint 被 429 → 設冷卻到 now + retry_after（從錯誤訊息解析或顯式給）。"""
        secs = retry_after if retry_after is not None else parse_retry_after(err_str)
        ep.cooldown_until = self._clock() + secs

    def record_usage(self, ep: PoolEndpoint, tokens: int) -> None:
        """呼叫成功後回報用量，進滾動 TPM 視窗 + ① 累計當日用量（跨日重置）。"""
        now = self._clock()
        tok = max(0, int(tokens))
        ep.usage_window.append((now, tok))
        if ep.daily_budget > 0:
            if now >= ep.daily_reset_at:   # 滾動 24h 視窗過期 → 重置
                ep.daily_used = 0
                ep.daily_reset_at = now + 86400.0
            ep.daily_used += tok

    def status(self) -> list[dict]:
        """每個 endpoint 的即時觀測（給 /marvin_system 算力池視圖）。

        只回 pool 真的知道的東西：滾動 60s TPM 用量 + 冷卻狀態。不估 TPD（本地計數
        必然低估、需 limit 表 + 持久化，會騙人）。狀態判定對齊 next_available 的跳過規則。
        """
        now = self._clock()
        rows: list[dict] = []
        for ep in self.endpoints:
            tpm = self.current_tpm(ep)
            budget = ep.tpm_budget or 1
            dratio = self.daily_ratio(ep)
            if now < ep.cooldown_until:
                st = "cooldown"
            elif tpm > ep.tpm_budget * self.TPM_HEADROOM:
                st = "tpm_high"
            elif dratio > self.DAILY_HEADROOM:
                st = "daily_high"
            else:
                st = "available"
            rows.append({
                "name": ep.name, "model": ep.model, "status": st,
                "cooldown_remaining": max(0.0, ep.cooldown_until - now),
                "tpm_used": tpm, "tpm_budget": ep.tpm_budget,
                "tpm_pct": round(tpm / budget * 100, 1),
                "daily_used": ep.daily_used, "daily_budget": ep.daily_budget,
                "daily_pct": round(dratio * 100, 1) if ep.daily_budget else None,
            })
        return rows


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


# ── Endpoint 工廠：讀 env 組池（缺 key 自動略過）─────────────────────────────

@dataclass(frozen=True)
class ProviderSpec:
    """一家 OpenAI-相容 provider 的設定。model 名可被 env 覆寫（免改 code）。"""
    name: str
    key_env: str
    base_url: str
    quick_model: str           # Tier 1（8b 級）預設
    analyze_model: str         # Tier 2（70b 級）預設
    quick_model_env: str = ""  # 空 → 用 {NAME}_QUICK_MODEL
    analyze_model_env: str = ""
    tpm_budget: int = 6000
    # ① 每日 token cap（TPD）。0 = 未知/無限制（不罰）。只有官方文件/實測 429 訊息
    # 確認過的才填，誠實不亂估。quick/analyze 用不同 model → daily cap 不同。
    quick_daily: int = 0
    analyze_daily: int = 0


# 優先序 = list 順序（next_available 按序回）。Groq/Cerebras 用既有 env 名（你的 key
# 會自動被撿）；三個新的（SambaNova/Together/OpenRouter）等填 key。model 名都可 env 覆寫，
# 因為各家命名不同、且 OpenRouter free 模型名會變。
_PROVIDERS: list[ProviderSpec] = [
    # Groq daily cap（6/2 從 429 訊息實測）：8b TPD 50萬、70b TPD 10萬。今天就是
    # 70b 先撞 10萬、8b 後撞 50萬。填上後 daily 快爆會自動讓位、不會用到炸。
    ProviderSpec("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1",
                 "llama-3.1-8b-instant", "llama-3.3-70b-versatile",
                 quick_model_env="GROQ_SIMPLE_MODEL", analyze_model_env="GROQ_FALLBACK_MODEL",
                 tpm_budget=6000, quick_daily=500000, analyze_daily=100000),
    # Cerebras 6/1 實測 /models 只剩 zai-glm-4.7 + gpt-oss-120b；舊的 llama3.1-8b /
    # qwen-3-235b-a22b-instruct-2507 已下架（404 model_not_found）。zai-glm-4.7 是
    # reasoning model 回 `reasoning` 非 `content` 跟 OpenAI 介面不兼容，所以兩檔都
    # 統一 gpt-oss-120b（JSON mode 實測 OK）；tier 區分由其他 provider 承擔。
    ProviderSpec("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1",
                 "gpt-oss-120b", "gpt-oss-120b",
                 quick_model_env="CEREBRAS_QUICK_MODEL", analyze_model_env="CEREBRAS_MODEL",
                 tpm_budget=60000),
    # SambaNova 無 3.1-8b；quick 用 Maverick-17B（快），analyze 用 3.3-70B（2026-05-21 實測 /models）
    ProviderSpec("sambanova", "SAMBANOVA_API_KEY", "https://api.sambanova.ai/v1",
                 "Llama-4-Maverick-17B-128E-Instruct", "Meta-Llama-3.3-70B-Instruct"),
    # Together 8b 要帶 'Meta-' 前綴（實測 /models）
    ProviderSpec("together", "TOGETHER_API_KEY", "https://api.together.xyz/v1",
                 "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    # OpenRouter 免費目錄會變（2026-05-21 實測）：無 3.1-8b。quick 用 llama-3.2-3b（快、
    # 末位 fallback），analyze 用免費 llama-3.3-70b。同 llama 家族保 cleaner prompt 一致性。
    ProviderSpec("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
                 "meta-llama/llama-3.2-3b-instruct:free", "meta-llama/llama-3.3-70b-instruct:free"),
    # Gemini free tier（GOOGLE_API_KEY）走官方 OpenAI-compat 端點。獨立每日 quota，
    # 跟 Groq/Cerebras 分攤。6/2 加：flash-lite 快、免費。付費 Gemini 另走 _call_cloud C 兜底。
    ProviderSpec("gemini_free", "GOOGLE_API_KEY",
                 "https://generativelanguage.googleapis.com/v1beta/openai/",
                 "gemini-2.0-flash-lite", "gemini-2.0-flash"),
]


def _default_client_factory(base_url: str, api_key: str) -> Any:
    """預設用 openai.AsyncOpenAI（lazy import，免模組載入就依賴 openai）。"""
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


def _resolve_model(env: Mapping[str, str], spec: ProviderSpec, *, analyze: bool) -> str:
    override_env = spec.analyze_model_env if analyze else spec.quick_model_env
    override_env = override_env or f"{spec.name.upper()}_{'ANALYZE' if analyze else 'QUICK'}_MODEL"
    return env.get(override_env) or (spec.analyze_model if analyze else spec.quick_model)


def build_tier_pools(
    env: Optional[Mapping[str, str]] = None,
    *,
    client_factory: Callable[[str, str], Any] = _default_client_factory,
    clock: Callable[[], float] = time.time,
) -> tuple[CooldownAwarePool, CooldownAwarePool]:
    """讀 env 組 (quick_pool, analyze_pool)。有 key 的 provider 才進池，缺 key 略過。

    同一 provider 的 quick/analyze 共用一個 client（同 base_url+key，只差 model）。
    """
    env = os.environ if env is None else env
    quick_eps: list[PoolEndpoint] = []
    analyze_eps: list[PoolEndpoint] = []
    for spec in _PROVIDERS:
        key = env.get(spec.key_env)
        if not key:
            continue
        client = client_factory(spec.base_url, key)
        quick_eps.append(PoolEndpoint(
            name=f"{spec.name}-quick", client=client,
            model=_resolve_model(env, spec, analyze=False),
            tpm_budget=spec.tpm_budget, daily_budget=spec.quick_daily))
        analyze_eps.append(PoolEndpoint(
            name=f"{spec.name}-analyze", client=client,
            model=_resolve_model(env, spec, analyze=True),
            tpm_budget=spec.tpm_budget, daily_budget=spec.analyze_daily))
    return CooldownAwarePool(quick_eps, clock=clock), CooldownAwarePool(analyze_eps, clock=clock)


def build_tiered_router(
    env: Optional[Mapping[str, str]] = None,
    *,
    client_factory: Callable[[str, str], Any] = _default_client_factory,
    clock: Callable[[], float] = time.time,
) -> TieredLLMRouter:
    """一行組好 TieredLLMRouter（讀 env 的所有可用 provider）。"""
    quick_pool, analyze_pool = build_tier_pools(env, client_factory=client_factory, clock=clock)
    return TieredLLMRouter(quick_pool, analyze_pool)
