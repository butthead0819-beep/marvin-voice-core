# 為什麼我的免費 LLM 池晚上失敗率 30%？——三層假象偵錯記

> Devlog #2 · 2026-06-12 · 對應題庫 #3「LLM 成本」
> 一句話結論：**免費池不是不夠大，是你沒把失敗接好——而且你的監控數據可能在騙你。**

## 問題

Marvin（自架 Discord 語音陪伴 bot）跑 5 家免費 LLM provider 的 dispatch bus
（Groq / Cerebras / SambaNova / OpenRouter / Gemini free tier）。
日報顯示：安靜的白天失敗率 1%，熱鬧的晚上 30-41%，失敗原因 100% 是 429。

直覺解法是「再加 provider」或「砍用量」。都錯。查下去發現三層假象，
每一層都會把你帶去修錯的東西。

## 第一層假象：prod log 裡的「事故」是測試污染

log 裡有大量嚇人的紀錄：`LLMBus DEGRADED 可用 provider 僅剩 1 個`、
`'GeminiRouterLLMMixin' object has no attribute 'provider'`。

追進去發現 provider 名單寫著 `a=h, b=h`——真實 provider 叫 groq/cerebras，
`a`、`b` 是 **pytest fixture**。入口模組在 import 時就掛了
`RotatingFileHandler(bot_main.log)`，而某個測試 import 了入口模組
→ 跑整套測試時，所有測試的 WARNING 都灌進真的 prod log。

**修法**：import 時偵測 `"pytest" in sys.modules` 就跳過 logging 設定。
**教訓**：entry module 的 side effect（logging、stdout 劫持、env 寫入）
全部要 guard，否則測試一碰就污染 prod 觀測面。

## 第二層假象：最大用量戶 "wait_for" 根本不存在

日報的 per-purpose 歸因顯示最大戶是 `wait_for`（43 筆/天）。
我們的歸因是自動的：`sys._getframe(1).f_code.co_name` 抓直接 caller 名。

但只要呼叫長這樣：

```python
content = await asyncio.wait_for(
    router._call_llm(SYSTEM, prompt, tier="simple"),
    timeout=8.0,
)
```

coroutine 實際被驅動時，frame 1 是 `asyncio.wait_for` 本人。
三個背景任務（情緒分類、會話摘要、回憶查詢）就這樣隱身在同一個假名字下。

**修法**：這三個呼叫點顯式傳 `purpose=`。附帶紅利：其中兩個 purpose
本來就在「背景降權」名單裡——歸因修對的瞬間，高峰時段它們自動讓出
最稀缺的 Groq 額度給即時回應，等於免費拿到一個降載機制。
**教訓**：frame 自動歸因遇到 `asyncio.wait_for` / `gather` 包裹就會說謊；
歸因數據要先驗純度再拿來做容量決策。

## 第三層才是真兇：贏家 429 整筆死，別家在旁邊閒著

把 6 月的 1097 筆失敗逐筆撈出來看，全是同一個模式：

1. bus 收 bid，5 家裡選出信心最高的贏家
2. 贏家 `handle()` 打出去 → 429
3. **整筆 dispatch 直接 raise，請求死亡**
4. 下一筆 dispatch 才會避開冷卻中的贏家

bid 階段明明還有別家活著（live 探測：SambaNova 完全正常、OpenRouter 間歇可用），
但它們只在「下一筆」才有機會。高峰期 Groq+Cerebras 輪流 429，
於是 30% 的請求死在兩家主力的冷卻間隙裡。

**修法**（核心 diff 概念）：

```python
# before：viable[0] 一家定生死
winner = viable[0]
result = await winner.handle(ctx)   # 429 → raise → 請求死亡

# after：同一筆 dispatch 內沿 viable 名單 failover
for agent, bid, conf in viable:     # confidence 降序
    try:
        return await agent.handle(ctx)
    except Exception as e:
        last_exc = e                # agent 內部已 mark cooldown
        continue                    # 換下一家，無 TPM 雙計
raise last_exc                      # 全滅才放棄
```

**教訓**：multi-provider 路由的可靠度不在「有幾家」，
在「第一家失敗之後發生什麼」。free tier 的 429 是常態不是異常，
把它當成 dispatch 流程的一級公民，5 家紙面容量才會變成真容量。

## 順帶：免費 provider 實測現況（2026-06）

| Provider | 實測 | 備註 |
|---|---|---|
| Groq | 主力，高峰 429 | 快、額度有限 |
| Cerebras | 主力，高峰 429 | RPM 近無限但也會擠 |
| SambaNova | ✓ 完全正常 | 被埋沒的真備援 |
| OpenRouter `:free` | 間歇 | 上游被全網搶爆，failover 下堪用 |
| Gemini free | 幾乎不可用 | 同一把 key 的其他用途把每日額度吃光 |

## 程式碼

全部開源：https://github.com/butthead0819-beep/marvin-voice-core
對應 commits：`2e16f5c`（log guard）、`b2c1ce5`（failover）、`8162065`（歸因修正）

---

## 附：社群貼文草稿

### X（英文 thread，4 則）

**1/**
My self-hosted Discord voice bot runs 5 free-tier LLM providers behind a dispatch bus. Every busy evening: 30% failure rate, all 429s.

The fix wasn't "add more providers". I had to dig through three layers of illusion first 🧵

**2/**
Illusion 1: half the "incidents" in my prod log weren't real.

My entry module attached a log file handler at import time. One test imported it → every pytest run sprayed fake provider errors into the real prod log. Guard your entry-module side effects.

**3/**
Illusion 2: my #1 LLM consumer was labeled "wait_for". It doesn't exist.

Purpose auto-attribution grabbed the caller frame — which was asyncio.wait_for(), not the real caller. Three background tasks were hiding under one fake name, dodging my peak-hour demotion rules.

**4/**
The real bug: the bus picks a winner, winner gets 429, the whole request dies — while 3 healthy providers sit idle until the *next* dispatch.

Fix: fail over within the same dispatch. On free tiers, 429 is a first-class citizen, not an exception.

Code: https://github.com/butthead0819-beep/marvin-voice-core

### Threads（中文，487 字）

我的 Discord 語音 bot 掛了 5 家免費 LLM，晚上失敗率還是 30%。查下去發現三層假象，每層都差點讓我修錯東西。

第一層：log 裡的「事故」一半是假的。入口模組 import 時就掛 log handler，跑測試時整套測試的錯誤全灌進 prod log，我盯著 pytest 的假資料查「線上事故」。

第二層：用量報表最大戶叫 "wait_for"，查無此人。自動歸因抓 caller frame，但呼叫被 asyncio.wait_for() 包住，抓到的是 asyncio 本人。三個背景任務躲在假名字下，躲過了高峰降載規則。

第三層才是真兇：bus 選出贏家後，贏家吃到 429，整筆請求直接死——旁邊三家健康的 provider 只能等下一筆。高峰期兩家主力輪流 429，30% 的請求死在冷卻間隙裡。

修法一行話：429 就換下一家，同筆請求內 failover，全滅才放棄。

教訓：免費池不是不夠大，是你沒把失敗接好。free tier 的 429 是常態，當成一級公民處理，紙面容量才是真容量。

（程式碼開源，連結在 bio）
