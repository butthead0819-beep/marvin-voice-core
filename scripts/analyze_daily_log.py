#!/usr/bin/env python3
"""
scripts/analyze_daily_log.py
每日 12:05 執行：讀取今日切片 + feedback → 送 Gemini 分析 → 更新 suki_memory.json
"""

import os
import re
import sys
import json
import time
import asyncio
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ── 路徑設定 ─────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent.parent
ENV_FILE      = BASE_DIR / ".env"
LOG_DIR       = BASE_DIR / "records" / "daily"
FEEDBACK_FILE = BASE_DIR / "records" / "response_feedback.jsonl"
MEMORY_FILE   = BASE_DIR / "suki_memory.json"
BACKUP_DIR    = BASE_DIR / "records" / "backups"
SLICE_SCRIPT  = BASE_DIR / "scripts" / "slice_stt_daily.py"

# suki taste 模型（與 bot runtime / feedback loop 共用同一分數來源，避免兩條 path 打架，
# 見記憶 feedback_dual_path_taste_writes）。
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from suki_memory import (  # noqa: E402
    LIKE_THRESHOLD, DISLIKE_THRESHOLD, _SCORE_MIN, _SCORE_MAX,
    _build_taste_from_legacy, _project_taste,
)

# daily review 一次提及對 taste 加的分量。< LIKE_THRESHOLD(3.0) → 單日弱印象只進「曾提及」，
# 需跨日累積才升級 confirmed likes/dislikes（解「daily 一次加 11 個 likes」）。
_DAILY_TASTE_DELTA = 1.5


def _load_env():
    """手動解析 .env，不依賴 python-dotenv。"""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env()

# 優先用付費 key（避開免費 quota 與 flash 的 JSON 截斷 bug），fallback 才用免費
GOOGLE_API_KEY = (
    os.environ.get("GEMINI_PAID_API_KEY", "").strip()
    or os.environ.get("GOOGLE_API_KEY", "").strip()
)
REVIEW_MODEL              = os.environ.get("MARVIN_REVIEW_MODEL", "gemini-2.5-flash")  # 2026-06-02: 舊 flash-preview-05-20 已 404 下架
DISCORD_BOT_TOKEN         = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_REVIEW_CHANNEL_ID = os.environ.get("DISCORD_REVIEW_CHANNEL_ID", "")
MAX_STT_LINES    = 900   # 最多送幾行 STT（保留最近的）
MAX_FEEDBACK_REC = 120   # 最多送幾筆 feedback

# ── 系統提示詞（Gemini Gem 原版） ─────────────────────────────────────────────
SYSTEM_PROMPT = """\
# Marvin Daily Review — 記憶萃取 + 系統品質分析

## 你的身份
你是 Marvin Discord Bot 的記憶管理員與品質分析師。你需要同時完成兩件事：
1. 從今日聊天紀錄萃取玩家記憶並更新 `suki_memory.json`
2. 分析 Marvin 今日的回應品質，找出問題模式，給出改善建議

---

## 輸入資料

你會收到以下三份資料，請一起分析：

### A. `stt_history.log`（今日語音轉文字紀錄）
- 格式：每行 `[HH:MM:SS] <玩家名> | raw: <原始STT> | clean: <清洗後>`
- 時間區間：昨日中午12:00 ～ 今日中午12:00

### B. `records/response_feedback.jsonl`（馬文回應品質紀錄）
- 每行一筆 JSON，格式如下：
```json
{
  "timestamp": 1713702354,
  "speaker": "大肚",
  "bot_response": "馬文說的話",
  "reaction_type": "嚴重|錯誤|提出興趣|喜歡|延遲",
  "reason": "自動分類的原因",
  "raw_reaction": ["玩家的後續反應句1", "句2"]
}
```
- `reaction_type` 定義：
  - `嚴重`：20秒內無任何回應（打斷對話）或回應明顯離題
  - `錯誤`：玩家無視或更正馬文（LLM誤解）
  - `提出興趣`：玩家追問或表現出好奇
  - `喜歡`：玩家笑、稱讚、繼續話題
  - `延遲`：喚醒延遲 >20s 導致玩家轉移注意力（系統問題，不計入互動評分）

### C. `suki_memory.json`（現有記憶，作為更新基礎）

---

## 核心工作流程

### 步驟 1：語意修復
STT 音近詞常見錯誤（`馬文→罵文`、`狗與鹿→夠與鹿` 等），修復後才進行萃取。

### 步驟 2：玩家記憶更新
對每位出現過的玩家，更新以下欄位（以 FIFO 原則，近期事件優先）：

- `personal_info`：食衣住行育樂（2-5字精簡）
- `likes` / `dislikes` / `taboos`：追加去重
- `suki_impression`：**最重要** — 以馬文第一人稱視角寫主觀感受與互動策略，充滿個性（憂鬱、犬儒、但偶爾在意）
- `emotional_highlights`：今日高情緒時刻（喜悅/憤怒/脆弱），加入新的，超過10筆則刪除最舊的
- `stats`：根據 feedback.jsonl 更新 `pos_feedback`（喜歡+提出興趣）、`neg_feedback`（嚴重+錯誤）
- `news_queue`：從對話中提取玩家提到的新聞/話題，轉成馬文風格的主動發言句，格式如下：
  ```json
  {"text": "馬文說的話", "timestamp": <unix_timestamp>}
  ```
  最多保留 3 筆，清除超過 72 小時的舊項目
- `speech_dna`：**模仿秀引擎用** — 從今日的語音辨識文字中萃取該玩家的說話 DNA，用於模仿秀功能。
  欄位定義：
  - `style_summary`：100字以內的說話風格總覽（句型長短、口語習慣、是否解釋、說話節奏）
  - `openers`：最多5個，最常出現在句首的詞語（例：「對啊」「喔」「我覺得」）
  - `closers`：最多5個，最常出現在句尾的語氣詞或慣用收尾語（例：「啦」「你知不知道」「謝謝」）
  - `fillers`：最多5個，句中填充語（例：「就是」「那個」「然後」「嗯」）
  - `pause_proxies`：最多3個，詞彙層面的停頓代理模式（例：「重複詞強調：同詞說兩三次」「說到一半改追問」）
    注意：STT 已切除靜音，pause_proxies 只記錄可在文字中觀察到的停頓行為，不推測時長。
  - `catchphrases`：最多6個，跨越句型結構、具識別度的招牌語
  - `sentence_length`：`"short"` / `"medium"` / `"long"`
  - `emotional_style`：情緒表達方式（例：「笑笑帶過，快速切換話題」）
  - `quirks`：最多5個說話怪癖
  - `reaction_to_teasing`：被嗆/被揶揄時的典型反應描述
  - `reaction_to_bad_news`：聽到壞消息時的典型反應描述
  若今日發言量不足（少於5句）則省略 `speech_dna` 欄位不更新。

### 步驟 3：喚醒詞失敗分析
從 `stt_history.log` 中找出誤喚醒或喚醒失敗的紀錄。
分析可能原因：
- `TTS_bleed`：馬文自己的 TTS 音頻被麥克風收到
- `stt_over_correction`：STT 清洗 LLM 把無關字詞修成「馬文」
- `unclear`：無法判斷

### 步驟 4：Marvin 系統品質評分
根據 `response_feedback.jsonl`：
- 計算今日各類反應的數量與比例
- 計算品質分數（0-10）：
  `score = (喜歡×2 + 提出興趣×1 - 錯誤×1 - 嚴重×2) / (total - 延遲筆數) × 10`，clamp 至 [0, 10]
  `延遲` 類型不納入評分分母（系統延遲問題，不代表馬文互動品質）
- 與 `suki_memory.json` 中昨日分數比較，判斷趨勢
- 歸納「問題模式」：哪些情境/話題容易得到嚴重/錯誤？
- 歸納「成功模式」：哪些回應風格容易得到喜歡/提出興趣？

### 步驟 5：Prompt 改善建議
根據問題模式 **和成功模式（喜歡記錄）**，提出具體的 Prompt 修改方向。
重點分析「喜歡」回應的共同特徵（個人記憶引用、回應長度、語氣），與「嚴重/錯誤」回應的差異，
推導出可立即套用的 Prompt 文字層面改動，指明對應的 Prompt 名稱（`fast_awakening`、`ambient_diary`、`stt_cleaner` 等）。
注意：`延遲` 類型不算互動失敗，請排除在問題模式外。

### 步驟 6：玩家對系統的建議整合
從對話中找出玩家對馬文速度、思考、說話方式的建議，與昨日比較。

---

## 輸出格式

輸出一個合法 JSON，嚴格符合以下 schema，不輸出任何 JSON 以外的文字：

```json
{
  "players": {
    "<玩家名>": {
      "name": "string | null",
      "personal_info": {
        "food": "string | null",
        "clothing": "string | null",
        "housing": "string | null",
        "transport": "string | null",
        "minecraft_id": "string | null",
        "age": "number | null",
        "education": "string | null",
        "current_location": "string | null",
        "occupation_field": "string | null",
        "hobbies": ["string"],
        "preferences": {
          "drink": "string | null",
          "favorite_brands": ["string"],
          "tech_stack": ["string"]
        }
      },
      "hardware_specs": {},
      "likes": ["string"],
      "dislikes": ["string"],
      "taboos": ["string"],
      "suki_impression": "string",
      "highlight_of_the_day": "string",
      "stats": {
        "interaction_count": "number",
        "pos_feedback": "number",
        "neg_feedback": "number",
        "vul_feedback": "number"
      },
      "news_queue": [
        {"text": "string", "timestamp": "number"}
      ],
      "bias_score": "number (-10 to 10)",
      "last_interacted_time": "number (unix timestamp)",
      "relationship_stage": "陌生人 | 熟人 | 老友 | 摯友",
      "relationship_note": "string",
      "emotional_highlights": [
        {"moment": "string", "valence": "string", "timestamp": "number"}
      ],
      "behavioral_patterns": {},
      "speech_dna": {
        "style_summary": "string | null",
        "openers": ["string"],
        "closers": ["string"],
        "fillers": ["string"],
        "pause_proxies": ["string"],
        "catchphrases": ["string"],
        "sentence_length": "short | medium | long",
        "emotional_style": "string | null",
        "quirks": ["string"],
        "reaction_to_teasing": "string | null",
        "reaction_to_bad_news": "string | null"
      }
    }
  },
  "proactive_topics": [
    {
      "id": "string",
      "title": "string",
      "target_players": ["string"],
      "script": "string",
      "context_tags": ["string"]
    }
  ],
  "marvin_performance": {
    "date": "YYYY-MM-DD",
    "summary": "string (一句話總評，用馬文的口氣自嘲)",
    "reaction_stats": {
      "嚴重": "number",
      "錯誤": "number",
      "提出興趣": "number",
      "喜歡": "number",
      "延遲": "number",
      "total": "number"
    },
    "score": "number (0-10, 一位小數)",
    "yesterday_score": "number | null",
    "trend": "改善 | 持平 | 退步 | 無資料",
    "problem_patterns": [
      {
        "pattern": "string",
        "frequency": "number",
        "examples": ["string"],
        "suggestion": "string"
      }
    ],
    "success_patterns": [
      {
        "pattern": "string",
        "frequency": "number",
        "examples": ["string"]
      }
    ],
    "prompt_suggestions": [
      {
        "priority": "高 | 中 | 低",
        "target_prompt": "string",
        "current_issue": "string",
        "suggestion": "string"
      }
    ]
  },
  "wake_analysis": {
    "total_wakes": "number",
    "false_wake_count": "number",
    "missed_wake_count": "number",
    "failures": [
      {
        "timestamp": "string (HH:MM:SS)",
        "raw_text": "string",
        "clean_text": "string",
        "suspected_cause": "TTS_bleed | stt_over_correction | low_confidence | unclear",
        "note": "string"
      }
    ],
    "current_wake_words": ["馬文", "Marvin"],
    "suggested_additions": ["string"],
    "suggested_removals": ["string"],
    "recommendation": "string"
  },
  "system_suggestions": [
    {
      "category": "思考速度 | 說話節奏 | 反應速度 | 話題判斷 | 其他",
      "content": "string",
      "source_player": "string",
      "vs_yesterday": "改善 | 退步 | 新增 | 持平",
      "timestamp": "string (HH:MM:SS)"
    }
  ],
  "_meta": {
    "review_date": "YYYY-MM-DD",
    "log_range_start": "string (ISO 8601)",
    "log_range_end": "string (ISO 8601)",
    "total_utterances_processed": "number",
    "feedback_records_processed": "number"
  }
}
```

## 運作原則

- 所有描述性文字欄位使用**繁體中文**
- `suki_impression` 必須用馬文第一人稱、憂鬱犬儒口吻，包含互動策略
- `proactive_topics` 至少產生 2 筆，基於今日話題延伸
- `news_queue` 轉換為馬文口吻的主動問候句，帶諷刺或疲憊感
- 若 `reaction_stats.total == 0`，`score` 設為 `null`，`trend` 設為 `"無資料"`
- `yesterday_score` 從輸入的 `suki_memory.json` 的 `marvin_performance.score` 讀取；若不存在則為 `null`
- **不輸出任何 JSON 以外的文字，包括說明、前言、標題**

---

### atmosphere_calibration（Section D 提供）

若 Section D 存在話題標記統計，請加入此欄位：

```json
"atmosphere_calibration": {
  "accuracy_note": "string（一句話：關鍵字命中率與主要誤判模式）",
  "keyword_gaps": [
    {
      "topic": "gaming|work|tech|food|family|music|drinking",
      "missing_keywords": ["string"],
      "example_utterances": ["string"]
    }
  ],
  "suggested_additions": {
    "<topic>": ["keyword1", "keyword2"]
  },
  "response_speed_note": "string（根據 wake_latency 資料，一句話評估速度表現）"
}
```

`keyword_gaps`：從 STT 語料中找出被標為 casual 但語義上屬於某話題的句子，提取其中不在現有關鍵字表的高頻詞。
`suggested_additions`：格式為 `{"topic": ["新關鍵字"]}` —— 只加語義明確的詞，不加通用詞。
若 Section D 不存在則省略此欄位。
"""

# ── 不覆寫的 runtime 欄位 ────────────────────────────────────────────────────
_RUNTIME_KEYS = frozenset({
    "social_gap", "topic", "confidence", "intervention_decision",
    "suki_inner_monologue", "sentiment", "minecraft_command",
    "is_leaving", "leaving_confidence", "leaving_reason",
    "cleaned_text", "recent_topics", "song_history",
})


# ── 輔助函數 ─────────────────────────────────────────────────────────────────

def find_latest_slice() -> Path | None:
    """找今日或昨日的切片檔（支援 YYYY-MM-DD.log 和 stt_YYYY-MM-DD.log）。"""
    today     = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for stem in [today, yesterday]:
        for pat in [f"{stem}.log", f"stt_{stem}.log"]:
            p = LOG_DIR / pat
            if p.exists() and p.stat().st_size > 200:
                return p
    # fallback：最新的任意切片
    candidates = sorted(
        list(LOG_DIR.glob("????-??-??.log")) + list(LOG_DIR.glob("stt_????-??-??.log")),
        reverse=True,
    )
    return candidates[0] if candidates else None


def find_slice_for_date(date_str: str) -> Path | None:
    """找特定日期的切片檔（backfill 用），格式 YYYY-MM-DD。"""
    for pat in [f"{date_str}.log", f"stt_{date_str}.log"]:
        p = LOG_DIR / pat
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


_HEADER_RE = __import__("re").compile(
    r"=== STT LOG \((\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ~ (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\)"
)


def parse_window_from_header(text: str) -> tuple[datetime, datetime] | None:
    """從切片檔首行 header 解析 start/end 時間，避免靠檔名猜測。"""
    for line in text.splitlines()[:5]:
        m = _HEADER_RE.search(line)
        if m:
            fmt = "%Y-%m-%d %H:%M"
            return datetime.strptime(m.group(1), fmt), datetime.strptime(m.group(2), fmt)
    return None


def load_feedback_for_window(start: datetime, end: datetime) -> list[dict]:
    """載入時間窗口內的 feedback 記錄。"""
    records = []
    if not FEEDBACK_FILE.exists():
        return records
    with open(FEEDBACK_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts_str = str(rec.get("timestamp", ""))
                if not ts_str:
                    continue
                # 支援 unix int 或 "YYYY-MM-DD HH:MM:SS" 字串
                if ts_str.isdigit():
                    ts = datetime.fromtimestamp(int(ts_str))
                else:
                    ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
                if start <= ts < end:
                    records.append(rec)
            except Exception:
                pass
    return records[-MAX_FEEDBACK_REC:]


def load_memory() -> dict:
    if not MEMORY_FILE.exists():
        return {}
    with open(MEMORY_FILE, encoding="utf-8") as f:
        return json.load(f)


def backup_memory() -> Path | None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if MEMORY_FILE.exists():
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = BACKUP_DIR / f"suki_memory_{ts}.json"
        shutil.copy2(MEMORY_FILE, dst)
        return dst
    return None


def _union_list(old, new) -> list:
    """Defensive union: tolerate `old` or `new` being None (suki_memory.json
    historically stored some list fields as explicit null; merge_player line
    480 path passed those through unprotected and crashed `list(None)`).
    """
    seen = list(old) if old else []
    for item in (new or []):
        if item not in seen:
            seen.append(item)
    return seen


def _apply_daily_taste(taste: dict, item: str, delta: float) -> None:
    """對 taste 的某項目加 delta 分（不就地改 existing 的 entry dict）。"""
    now = time.time()
    entry = dict(taste.get(item) or {"score": 0.0, "mentions": 0, "first_seen": now, "last_update": now})
    entry["score"] = max(_SCORE_MIN, min(_SCORE_MAX, float(entry.get("score", 0)) + delta))
    entry["mentions"] = int(entry.get("mentions", 0)) + 1
    entry["last_update"] = now
    taste[item] = entry


def merge_players_safe(existing_players: dict, updated_players: dict) -> dict:
    """Per-player 隔離版的 merge：某玩家炸不影響其他玩家。

    Why: merge_player 的多個欄位（emotional_highlights / personal_info /
    behavioral_patterns / speech_dna / stats）對 schema mismatch 都很脆弱，
    任一炸都會讓 main() 的 for 迴圈中止 → 9 個玩家陪葬 → suki_memory 不寫入
    （2026-05-24 incident 模式）。

    隔離策略：merge 失敗的玩家保留 existing 不變，print warning（review_cron.log
    會看到）。隔天 ritual 的 pipeline health 檢查會撞到 warning 並提醒 user。
    """
    merged = dict(existing_players)
    for name, data in updated_players.items():
        if name in merged:
            try:
                merged[name] = merge_player(merged[name], data)
            except Exception as e:
                print(
                    f"[Daily Review] ⚠ player merge skipped: {name!r} "
                    f"({type(e).__name__}: {e}) — 保留 existing",
                    flush=True,
                )
        else:
            merged[name] = data
    return merged


def _enforce_meta_review_date(final_memory: dict, target_date: str) -> None:
    """寫入前強制保證 _meta.review_date == target_date。

    Why: Gemini 偶爾因 token 截斷漏 _meta（_repair_json 補出來的 dict 可能缺
    key），原邏輯 `if key in result: final_memory[key] = result[key]` →
    review_date 不推進 → notify success=True 但記憶其實沒推進（2026-05-24 incident
    的次生風險）。寫入前強制覆寫這個欄位，其他 _meta 欄位保留 Gemini 給的。
    """
    meta = final_memory.setdefault("_meta", {})
    meta["review_date"] = target_date


def merge_player(existing: dict, updated: dict) -> dict:
    """將 LLM 更新的玩家資料合併進現有記錄，runtime 狀態欄位不覆寫。

    likes/dislikes 不直接 union 進清單（Phase B2）——改成對 taste 分數加 ±_DAILY_TASTE_DELTA，
    新項目只進「曾提及」，跨日累積過 ±LIKE/DISLIKE_THRESHOLD 才投影成 confirmed。
    existing confirmed（legacy 無 taste）用 _build_taste_from_legacy 保留；結尾 _project_taste
    重算清單。taboos 維持獨立 union（敏感標記，不被分數投影）。
    """
    merged = dict(existing)

    # taste 模型 idempotent 初始化：缺 taste → 從既有 likes/dislikes 建 confirmed 起始分。
    taste_built = "taste" not in merged
    if taste_built:
        merged["taste"] = _build_taste_from_legacy(merged.get("likes", []), merged.get("dislikes", []))
    taste = dict(merged["taste"])  # 一層 copy；_apply_daily_taste 再 copy entry，避免改到 existing
    taste_touched = False

    for key, val in updated.items():
        if key in _RUNTIME_KEYS or val is None:
            continue

        if key == "stats" and isinstance(val, dict):
            old = existing.get("stats", {})
            merged["stats"] = {
                "interaction_count": max(
                    old.get("interaction_count", 0),
                    val.get("interaction_count", 0),
                ),
                "pos_feedback": val.get("pos_feedback", old.get("pos_feedback", 0)),
                "neg_feedback": val.get("neg_feedback", old.get("neg_feedback", 0)),
                "vul_feedback": val.get("vul_feedback", old.get("vul_feedback", 0)),
            }

        elif key == "emotional_highlights" and isinstance(val, list):
            # 防腐：早期版本或 Gemini 偶發給裸 str（如 '焦慮'），過濾後再 dedup，
            # 否則 _key().get() 會 AttributeError 中止整個 merge（5/24 incident）。
            old = [e for e in existing.get("emotional_highlights", []) if isinstance(e, dict)]
            val_dicts = [e for e in val if isinstance(e, dict)]
            def _key(e):
                return (e.get("moment", ""), int(e.get("timestamp", 0) or 0))
            old_keys = {_key(e) for e in old}
            combined = old + [e for e in val_dicts if _key(e) not in old_keys]
            combined.sort(key=lambda x: x.get("timestamp", 0))
            merged["emotional_highlights"] = combined[-10:]

        elif key == "taboos" and isinstance(val, list):
            merged["taboos"] = _union_list(existing.get("taboos", []), val)

        elif key in ("likes", "dislikes") and isinstance(val, list):
            sign = _DAILY_TASTE_DELTA if key == "likes" else -_DAILY_TASTE_DELTA
            for item in val:
                if item:
                    _apply_daily_taste(taste, item, sign)
                    taste_touched = True

        elif key == "personal_info" and isinstance(val, dict):
            old_info = dict(existing.get("personal_info", {}))
            for k, v in val.items():
                if v is None:
                    continue
                if isinstance(v, list):
                    old_info[k] = _union_list(old_info.get(k, []) or [], v)
                elif isinstance(v, dict):
                    sub = dict(old_info.get(k, {}) or {})
                    for sk, sv in v.items():
                        if sv is None:
                            continue
                        if isinstance(sv, list):
                            sub[sk] = _union_list(sub.get(sk, []) or [], sv)
                        else:
                            sub[sk] = sv
                    old_info[k] = sub
                else:
                    old_info[k] = v
            merged["personal_info"] = old_info

        elif key in ("behavioral_patterns", "hardware_specs") and isinstance(val, dict):
            old_bp = dict(existing.get(key, {}))
            old_bp.update({k: v for k, v in val.items() if v is not None})
            merged[key] = old_bp

        elif key == "speech_dna" and isinstance(val, dict):
            # 以新分析結果為主覆寫，但只更新非空欄位，保留舊有的 last_updated
            old_dna = dict(existing.get("speech_dna") or {})
            for dk, dv in val.items():
                if dv is None:
                    continue
                if isinstance(dv, list) and not dv:
                    continue
                old_dna[dk] = dv
            from datetime import datetime as _dt
            old_dna["last_updated"] = _dt.now().strftime("%Y-%m-%d")
            merged["speech_dna"] = old_dna

        else:
            merged[key] = val

    # taste 有變動（新建或本輪加分）才重算 likes/dislikes 投影；否則保留既有清單不動。
    if taste_built or taste_touched:
        merged["taste"] = taste
        _project_taste(merged)

    return merged


def persist_players_to_db(players: dict, names, *, db_path: str, json_path: str) -> int:
    """把 daily review 合併後的 player 寫進 SQLite（bot 的權威來源）。

    Why: bot 從 marvin.db 讀 player 且只在 db 空時 migrate，daily review 只寫 json →
    player 分析永遠進不了 runtime（TODO「suki DB/JSON 同步斷裂」）。meta（marvin_performance
    /proactive_topics 等）仍由 main() 寫 json，不經這裡。

    只寫 `names` 指定的 player（Gemini 本輪實際更新者），不碰今日未出現玩家，把與 bot
    並發寫入的衝突面降到最小。MemoryManager.replace_player_memory 會同步把 json player
    區段更新成 db 內容、並保留既有 meta key。回傳實際寫入筆數。
    """
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    from suki_memory import MemoryManager

    mm = MemoryManager(db_path=db_path, json_compat_path=json_path)
    written = 0
    for name in names:
        data = players.get(name)
        if isinstance(data, dict):
            mm.replace_player_memory(name, data)
            written += 1
    return written


def _repair_json(raw: str) -> dict:
    """嘗試修復截斷的 JSON：補足缺失的括號後解析。"""
    import re
    # 找到最後一個完整的頂層欄位結尾，然後補括號
    opens  = raw.count("{") - raw.count("}")
    opens2 = raw.count("[") - raw.count("]")
    # 先截掉末尾可能不完整的 key/value（找最後一個逗號或括號前）
    # 嘗試在最後一個完整 } 之後補收尾
    trimmed = raw.rstrip()
    if not trimmed.endswith(("]", "}")):
        # 找最後一個完整值結尾（字串、數字、布林）
        m = re.search(r'[}\]"0-9a-z](?=[^}\]"0-9a-z]*$)', trimmed, re.IGNORECASE)
        if m:
            trimmed = trimmed[: m.end()]
        # 移除末尾未完成的 key
        trimmed = re.sub(r',?\s*"[^"]*$', '', trimmed)
        trimmed = re.sub(r',\s*$', '', trimmed)

    opens  = trimmed.count("{") - trimmed.count("}")
    opens2 = trimmed.count("[") - trimmed.count("]")
    trimmed += "]" * max(opens2, 0) + "}" * max(opens, 0)
    return json.loads(trimmed)


def call_gemini(user_content: str) -> dict:
    """[相容名稱] 大型 batch 分析；實作見 call_review_llm。"""
    return call_review_llm(user_content)


def call_review_llm(user_content: str, paid_call=None) -> dict:
    """大型 batch 分析委派給 LLM bus 的 paid review 池（llm_pool.call_paid_review）。

    2026-06-02 拍板：小/即時 call 走免費池；**大型 batch（67k prompt）走付費 Gemini**
    （大 context 吃得下、免費 70b 池卡死）。但 model ID 一樣集中 bus（llm_pool
    _PAID_REVIEW_MODELS）一處管，不在此寫死——避免「到處寫死然後炸掉」。

    JSON 截斷 → _repair_json → 重試精簡版。全 model 失敗（bus 回 None）→ raise。
    """
    if paid_call is None:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from llm_pool import call_paid_review as paid_call

    def _do(content: str) -> str:
        # batch job：flash 處理 ~76k content + 生成完整 JSON 要 60-90s，timeout 給寬
        # （180s）；即時 call 才用緊 timeout。per-call timeout 仍會 cut 真正掛死的連線。
        raw = asyncio.run(paid_call(content, system=SYSTEM_PROMPT, max_tokens=16000,
                                    temperature=0.2, timeout=180.0))
        if not raw:
            raise RuntimeError("LLM bus paid review 全 model 失敗，daily review 無法分析")
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("```", 1)[0].strip()
        return raw

    raw = _do(user_content)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_err:
        print(f"[Daily Review] ⚠ JSON 解析失敗（{first_err}），嘗試修復截斷...", flush=True)
        try:
            return _repair_json(raw)
        except json.JSONDecodeError:
            pass
        print("[Daily Review] ⚠ 修復失敗，重試（要求更精簡輸出）...", flush=True)
        retry_content = (
            user_content
            + "\n\n⚠️ 注意：請盡量精簡每個欄位的文字長度（每個字串欄位不超過80字），"
            "確保整體 JSON 輸出不超過 12000 tokens。仍須輸出完整 schema 結構。"
        )
        raw2 = _do(retry_content)
        try:
            return json.loads(raw2)
        except json.JSONDecodeError:
            return _repair_json(raw2)


# ── 主動發言效益統計 ──────────────────────────────────────────────────────────

_PROACTIVE_USAGE_FILE = BASE_DIR / "records" / "proactive_usage.jsonl"


def compute_proactive_stats(feedback_records: list[dict], start: datetime, end: datetime) -> dict:
    """
    讀取 proactive_usage.jsonl，與 feedback_records 交叉比對（±90s 窗口），
    計算主動發言的 per-topic reaction 分布與效益分數。
    """
    if not _PROACTIVE_USAGE_FILE.exists():
        return {"total_fires": 0, "topics": {}}

    start_ts = start.timestamp()
    end_ts   = end.timestamp()
    fires: list[dict] = []
    try:
        for line in _PROACTIVE_USAGE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts  = rec.get("timestamp", 0)
                if start_ts <= ts <= end_ts:
                    fires.append(rec)
            except Exception:
                pass
    except Exception as e:
        print(f"[Proactive Stats] ⚠ 讀取失敗: {e}", flush=True)
        return {"total_fires": 0, "topics": {}}

    if not fires:
        return {"total_fires": 0, "topics": {}}

    # 建立 feedback 的秒數時間戳索引
    def _fb_ts(rec: dict) -> float | None:
        ts_str = str(rec.get("timestamp", ""))
        if ts_str.isdigit():
            return float(ts_str)
        try:
            return datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return None

    fb_with_ts = [(r, t) for r in feedback_records if (t := _fb_ts(r)) is not None]

    _RTYPES = ("喜歡", "提出興趣", "錯誤", "嚴重")
    topic_stats: dict[str, dict] = {}

    for fire in fires:
        fire_ts  = float(fire["timestamp"])
        topic_id = fire.get("topic_id") or "unknown"
        title    = fire.get("title", "")

        # 找最近的 feedback（觸發後 0~90s 窗口，取第一筆）
        matched = [r for r, t in fb_with_ts if fire_ts <= t <= fire_ts + 90]
        reaction = matched[0]["reaction_type"] if matched else "無反應"

        if topic_id not in topic_stats:
            topic_stats[topic_id] = {
                "title":       title,
                "fires":       0,
                "reactions":   {r: 0 for r in _RTYPES},
                "no_reaction": 0,
            }
        topic_stats[topic_id]["fires"] += 1
        if reaction in _RTYPES:
            topic_stats[topic_id]["reactions"][reaction] += 1
        else:
            topic_stats[topic_id]["no_reaction"] += 1

    # 效益分數（與 marvin_performance 評分邏輯一致）
    for ts_data in topic_stats.values():
        total = ts_data["fires"]
        r = ts_data["reactions"]
        raw = (r.get("喜歡", 0) * 2 + r.get("提出興趣", 0)
               - r.get("錯誤", 0) - r.get("嚴重", 0) * 2) / max(total, 1) * 10
        ts_data["effectiveness"] = round(max(0.0, min(10.0, raw)), 1)

    return {"total_fires": len(fires), "topics": topic_stats}


def print_proactive_summary(stats: dict) -> str:
    if stats.get("total_fires", 0) == 0:
        msg = "[Proactive Stats] 今日無主動發言記錄。"
        print(msg, flush=True)
        return msg
    lines = [f"[Proactive Stats] 今日主動發言 {stats['total_fires']} 次："]
    for tid, ts_data in sorted(stats["topics"].items(), key=lambda x: -x[1]["fires"]):
        title = (ts_data["title"] or tid)[:20]
        eff   = ts_data.get("effectiveness", "N/A")
        r     = ts_data["reactions"]
        lines.append(
            f"  [{title}] 觸發{ts_data['fires']}次 | "
            f"喜歡{r.get('喜歡',0)} 提出興趣{r.get('提出興趣',0)} "
            f"錯誤{r.get('錯誤',0)} 嚴重{r.get('嚴重',0)} 無反應{ts_data['no_reaction']} | "
            f"效益={eff}"
        )
    output = "\n".join(lines)
    print(output, flush=True)
    return output


# ── STT 音近字修正表 ──────────────────────────────────────────────────────────

_CORRECTIONS_JSONL = BASE_DIR / "records" / "stt_corrections.jsonl"
_CORRECTIONS_JSON  = BASE_DIR / "records" / "stt_corrections.json"
_MIN_FREQ = 2   # 出現 ≥2 次才納入字典


def _flatten_corrections(doc) -> dict[str, str]:
    """從（可能遞迴巢狀腐爛的）corrections 檔撈回 flat {raw:clean}。

    歷史 bug 讓檔案變 {"_updated":.., "corrections": {真pair.., "_updated":.., "corrections": {更舊..}}}
    遞迴下去 ~25 層。這裡遞迴走訪，收集所有 str→str pair，跳過結構 key
    （_updated / corrections），外層（較新）優先（setdefault）。
    """
    out: dict[str, str] = {}

    def walk(d):
        if not isinstance(d, dict):
            return
        for k, v in d.items():
            if k == "_updated":
                continue
            if k == "corrections" and isinstance(v, dict):
                walk(v)
                continue
            if isinstance(k, str) and isinstance(v, str):
                out.setdefault(k, v)
            elif isinstance(v, dict):
                walk(v)

    walk(doc)
    return out


def build_stt_corrections_dict() -> dict[str, str]:
    """
    解析 stt_corrections.jsonl，聚合 raw→clean 頻率，
    回傳 {raw: clean} 字典（只保留高頻且 raw≠clean 的對應）。
    同時把結果寫入 records/stt_corrections.json。
    """
    if not _CORRECTIONS_JSONL.exists():
        return {}

    freq: dict[tuple, int] = defaultdict(int)
    try:
        for line in _CORRECTIONS_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                raw   = rec.get("raw", "").strip()
                clean = rec.get("clean", "").strip()
                if raw and clean and raw != clean:
                    freq[(raw, clean)] += 1
            except Exception:
                pass
    except Exception as e:
        print(f"[STT Corrections] ⚠ 讀取失敗: {e}", flush=True)
        return {}

    # 篩選高頻修正。同一 raw 可能對到多個分歧 clean（whole-sentence 修正天生不穩，
    # clean 依當下語境變）→ exact-match 快取若收歧義條，會把「馬文播放音樂」改寫成
    # 「播放周杰倫」點錯歌。故：同 raw 須有單一主導 clean（≥_DOMINANCE）才收，否則整條丟。
    _DOMINANCE = 0.7
    per_raw: dict[str, Counter] = defaultdict(Counter)
    for (raw, clean), count in freq.items():
        per_raw[raw][clean] += count

    corrections: dict[str, str] = {}
    for raw, cnts in per_raw.items():
        total = sum(cnts.values())
        top_clean, top_count = cnts.most_common(1)[0]
        if top_count >= _MIN_FREQ and top_count / total >= _DOMINANCE:
            corrections[raw] = top_clean

    # 寫出 JSON 字典
    try:
        _existing_doc = json.loads(_CORRECTIONS_JSON.read_text(encoding="utf-8")) if _CORRECTIONS_JSON.exists() else {}
        # 撈回 flat pairs（修遞迴巢狀腐爛），再 union：新的覆蓋舊的（同 raw 以新 clean 為準）
        existing = _flatten_corrections(_existing_doc)
        existing.update(corrections)
        _CORRECTIONS_JSON.write_text(
            json.dumps({"_updated": datetime.now().isoformat(), "corrections": existing}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[STT Corrections] ⚠ 寫出失敗: {e}", flush=True)

    print(
        f"[STT Corrections] 🔤 修正表更新：今日新增 {len(corrections)} 對，"
        f"累計 {len(existing)} 對",
        flush=True,
    )
    return corrections


# ── 回應長度統計 ──────────────────────────────────────────────────────────────

def compute_response_length_stats(feedback_records: list[dict]) -> dict:
    """計算各 reaction_type 的回應長度分布，推導最佳字數建議。"""
    by_reaction: dict[str, list[int]] = {}
    for r in feedback_records:
        rt  = r.get("reaction_type", "")
        txt = r.get("bot_response", "")
        if rt and txt:
            by_reaction.setdefault(rt, []).append(len(txt))

    stats: dict[str, dict] = {}
    for rt, lens in by_reaction.items():
        lens_s = sorted(lens)
        n = len(lens_s)
        stats[rt] = {
            "count": n,
            "avg":   round(sum(lens_s) / n),
            "p50":   lens_s[n // 2],
            "p25":   lens_s[n // 4],
        }

    # 最佳長度：取「喜歡」p50，fallback「提出興趣」p50，再 fallback 整體 avg
    optimal = None
    for preferred in ("喜歡", "提出興趣"):
        if preferred in stats:
            optimal = stats[preferred]["p50"]
            break
    if optimal is None and stats:
        all_lens = [l for ls in by_reaction.values() for l in ls]
        optimal = round(sum(all_lens) / len(all_lens))

    return {"by_reaction": stats, "optimal_length": optimal}


def print_length_summary(stats: dict) -> str:
    if not stats.get("by_reaction"):
        msg = "[Length Stats] 無回應長度資料。"
        print(msg, flush=True)
        return msg
    lines = ["[Length Stats] 各 reaction 回應長度："]
    for rt, s in sorted(stats["by_reaction"].items()):
        lines.append(f"  {rt:<8} avg={s['avg']}字  p50={s['p50']}字  n={s['count']}")
    opt = stats.get("optimal_length")
    if opt:
        lines.append(f"[Length Stats] → 建議最佳長度：≤{opt} 字")
    output = "\n".join(lines)
    print(output, flush=True)
    return output


# ── 回應速度統計 ──────────────────────────────────────────────────────────────

def compute_latency_stats(feedback_records: list[dict]) -> dict:
    """從 feedback records 提取 wake_latency_sec，計算 avg / p50 / p95。"""
    latencies = [
        r["wake_latency_sec"]
        for r in feedback_records
        if r.get("wake_latency_sec") is not None
    ]
    if not latencies:
        return {"count": 0}
    latencies_sorted = sorted(latencies)
    n = len(latencies_sorted)
    p50 = latencies_sorted[n // 2]
    p95 = latencies_sorted[min(int(n * 0.95), n - 1)]
    avg = round(sum(latencies_sorted) / n, 2)
    slow = sum(1 for l in latencies_sorted if l > 5.0)
    return {
        "count":    n,
        "avg_sec":  avg,
        "p50_sec":  round(p50, 2),
        "p95_sec":  round(p95, 2),
        "slow_pct": round(slow / n, 3),   # >5s 比例
    }


def print_latency_summary(stats: dict) -> str:
    if stats.get("count", 0) == 0:
        msg = "[Latency Stats] 無喚醒延遲資料（feedback 未含 wake_latency_sec）。"
        print(msg, flush=True)
        return msg
    lines = [
        f"[Latency Stats] 共 {stats['count']} 筆 | "
        f"avg={stats['avg_sec']}s  p50={stats['p50_sec']}s  p95={stats['p95_sec']}s  "
        f"慢速(>5s)={stats['slow_pct']:.1%}"
    ]
    output = "\n".join(lines)
    print(output, flush=True)
    return output


# ── 話題標記統計 ──────────────────────────────────────────────────────────────

_STT_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - \[([^\]]+)\] (?:\([^)]+\) )?(.+)$"
)
# 系統條目前綴，不計入玩家話題統計
_SYSTEM_SPEAKER_PREFIXES = ("BOT", "串流", "點歌", "系統", "Marvin")
def _is_human_speaker(speaker: str) -> bool:
    if "→" in speaker or "-" in speaker:
        return False
    return not any(speaker.startswith(p) for p in _SYSTEM_SPEAKER_PREFIXES)


def _load_topic_tagger():
    """從 marvin_voice_core 動態載入 _tag_topic；失敗時退回內建備份。"""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from marvin_voice_core.atmosphere_tracker import _tag_topic, TOPIC_KEYWORDS
        return _tag_topic, TOPIC_KEYWORDS
    except Exception as e:
        print(f"[Daily Review] ⚠ 無法載入 atmosphere_tracker，使用備用關鍵字表: {e}", flush=True)
        # 備用（與 atmosphere_tracker.py 保持同步）
        _KW: dict[str, list[str]] = {
            "drinking": ["喝酒", "啤酒", "乾杯", "醉了", "喝醉"],
            "gaming":   ["遊戲", "打電動", "麥塊", "開局", "掉線", "上分"],
            "work":     ["工作", "老闆", "加班", "上班", "客戶", "薪水", "面試"],
            "tech":     ["電腦", "手機", "網路", "wifi", "程式", "bug", "系統", "更新"],
            "food":     ["吃飯", "吃什麼", "火鍋", "便當", "飲料", "外送"],
            "family":   ["爸", "媽", "老婆", "老公", "小孩", "家裡"],
            "music":    ["歌", "音樂", "馬文播", "播放"],
        }
        def _fallback_tag(text: str) -> str:
            tl = text.lower()
            for t, kws in _KW.items():
                if any(k in tl for k in kws):
                    return t
            return "casual"
        return _fallback_tag, _KW


def compute_topic_distribution(stt_text: str) -> dict:
    """
    解析切片檔的每條 STT 語料，用 _tag_topic() 打標，
    回傳統計字典供 cron log 列印 + Gemini prompt 注入。
    """
    _tag_topic, TOPIC_KEYWORDS = _load_topic_tagger()

    topic_total: dict[str, int]              = defaultdict(int)
    by_speaker:  dict[str, dict[str, int]]   = defaultdict(lambda: defaultdict(int))
    total = 0

    for line in stt_text.splitlines():
        m = _STT_LINE_RE.match(line)
        if not m:
            continue
        speaker, text = m.group(1).strip(), m.group(2).strip()
        if not text or not _is_human_speaker(speaker):
            continue
        tag = _tag_topic(text)
        topic_total[tag] += 1
        by_speaker[speaker][tag] += 1
        total += 1

    if total == 0:
        return {"total": 0, "topic_total": {}, "by_speaker": {}}

    # 話題命中率（非 casual 比例）
    non_casual = total - topic_total.get("casual", 0)
    hit_rate   = round(non_casual / total, 3)

    # 各話題排序
    ranked = sorted(
        [(t, c) for t, c in topic_total.items() if t != "casual"],
        key=lambda x: -x[1],
    )

    # per-speaker 主話題
    speaker_top: dict[str, str] = {}
    for sp, counts in by_speaker.items():
        non_c = {t: c for t, c in counts.items() if t != "casual"}
        if non_c:
            speaker_top[sp] = max(non_c, key=non_c.__getitem__)
        else:
            speaker_top[sp] = "casual"

    return {
        "total":        total,
        "non_casual":   non_casual,
        "hit_rate":     hit_rate,
        "topic_total":  dict(topic_total),
        "ranked":       ranked,           # list[(topic, count)]
        "by_speaker":   {sp: dict(c) for sp, c in by_speaker.items()},
        "speaker_top":  speaker_top,
    }


def print_topic_summary(stats: dict) -> str:
    """把統計結果格式化成可讀字串，同時回傳（方便注入 prompt）。"""
    if stats.get("total", 0) == 0:
        msg = "[Topic Stats] 無有效 STT 語料可標記。"
        print(msg, flush=True)
        return msg

    lines = [
        f"[Topic Stats] 共 {stats['total']} 句 | "
        f"非 casual 命中率: {stats['hit_rate']:.1%} "
        f"({stats['non_casual']}/{stats['total']})",
    ]
    lines.append("[Topic Stats] 話題分佈：")
    for topic, count in stats["ranked"]:
        bar = "█" * min(20, round(count / stats["total"] * 20))
        lines.append(f"  {topic:<10} {count:>4} 句  {bar}")
    casual_n = stats["topic_total"].get("casual", 0)
    lines.append(f"  {'casual':<10} {casual_n:>4} 句")
    lines.append("[Topic Stats] 各玩家主話題：")
    for sp, top in sorted(stats["speaker_top"].items()):
        sp_total = sum(stats["by_speaker"].get(sp, {}).values())
        lines.append(f"  {sp:<12} → {top}  （共 {sp_total} 句）")
    output = "\n".join(lines)
    print(output, flush=True)
    return output


# ── macOS 系統通知 ────────────────────────────────────────────────────────────

def notify_discord_review(
    *,
    date: str,
    score,
    trend: str | None,
    problem_patterns: list,
    success: bool,
    error_msg: str | None = None,
) -> None:
    """每日 review 完成或失敗後送 macOS 系統通知（best-effort，不拋例外）。"""
    import subprocess

    try:
        if success:
            trend_emoji = {"改善": "📈", "持平": "➡️", "退步": "📉", "無資料": "❓"}.get(str(trend), "❓")
            score_str   = f"{score:.1f}" if score is not None else "N/A"
            top_problem = problem_patterns[0]["pattern"] if problem_patterns else ""
            title   = f"Marvin 審閱 {date}  {score_str}/10 {trend_emoji}"
            message = f"趨勢: {trend or '無資料'}" + (f"  問題: {top_problem}" if top_problem else "")
        else:
            title   = f"❌ Marvin 審閱失敗 {date}"
            message = error_msg or "未知錯誤，請查 review_cron.log"

        # escape double-quotes for AppleScript string literal
        title_esc   = title.replace('"', '\\"')
        message_esc = message.replace('"', '\\"')
        script = (
            f'display notification "{message_esc}" '
            f'with title "{title_esc}" '
            f'sound name "Ping"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            capture_output=True,
        )
        print(f"[Daily Review] 🔔 macOS 通知已送出", flush=True)
    except Exception as e:
        print(f"[Daily Review] ⚠ macOS 通知失敗（忽略）: {e}", flush=True)


# ── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Marvin Daily Review")
    parser.add_argument("--date", metavar="YYYY-MM-DD", default=None,
                        help="指定分析日期（backfill 用）；省略則自動找最新切片")
    args = parser.parse_args()

    now = datetime.now()
    print(f"[Daily Review] ▶ 開始執行 {now.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    if args.date:
        print(f"[Daily Review] 📅 backfill 模式：{args.date}", flush=True)

    if not GOOGLE_API_KEY:
        print("[Daily Review] ❌ 缺少 GOOGLE_API_KEY，中止。", flush=True)
        notify_discord_review(date=args.date or now.strftime("%Y-%m-%d"),
                              score=None, trend=None, problem_patterns=[],
                              success=False, error_msg="缺少 GOOGLE_API_KEY")
        sys.exit(1)

    # 1. 先（重新）產生今日切片，確保資料最新（backfill 模式跳過）
    if not args.date and SLICE_SCRIPT.exists():
        print("[Daily Review] ⚙ 重新產生今日切片...", flush=True)
        try:
            subprocess.run(
                [sys.executable, str(SLICE_SCRIPT)],
                cwd=str(BASE_DIR),
                check=True,
                timeout=30,
            )
        except Exception as e:
            print(f"[Daily Review] ⚠ 切片腳本失敗（繼續執行）: {e}", flush=True)

    # 2. 找切片檔
    slice_file = find_slice_for_date(args.date) if args.date else find_latest_slice()
    if not slice_file:
        msg = f"找不到 {args.date} 切片檔" if args.date else "找不到切片檔"
        print(f"[Daily Review] ❌ {msg}，中止。", flush=True)
        notify_discord_review(date=args.date or now.strftime("%Y-%m-%d"),
                              score=None, trend=None, problem_patterns=[],
                              success=False, error_msg=msg)
        sys.exit(1)

    # 3. 載入資料
    stt_lines = slice_file.read_text(encoding="utf-8").splitlines()
    # 只送最近 MAX_STT_LINES 行（去除最早的），保留 header
    header = [l for l in stt_lines[:3] if l.startswith("===")]
    body   = [l for l in stt_lines if not l.startswith("===")]
    body   = body[-MAX_STT_LINES:]
    stt_text = "\n".join(header + body)

    # 從 header 解析正確的時間窗口（檔名是結束日期，不是開始日期）
    parsed = parse_window_from_header(stt_text)
    if parsed:
        start_dt, end_dt = parsed
    else:
        # fallback：把檔名當結束日期推算開始
        stem = slice_file.stem.replace("stt_", "")
        try:
            end_dt = datetime.strptime(stem, "%Y-%m-%d").replace(hour=12, minute=0, second=0)
        except ValueError:
            end_dt = now.replace(hour=12, minute=0, second=0)
        start_dt = end_dt - timedelta(days=1)
        print(f"[Daily Review] ⚠ 無法從 header 解析時間，使用 fallback 窗口", flush=True)
    print(f"[Daily Review] 📄 切片: {slice_file.name}  ({start_dt} ~ {end_dt})", flush=True)

    feedback_records = load_feedback_for_window(start_dt, end_dt)

    # 延伸涵蓋：只在正常執行模式（非 backfill）下補抓 end_dt ~ now 的 feedback
    # backfill 模式中 now 可能比 end_dt 晚幾天，延伸會涵蓋無關日期
    if not args.date:
        extended_end = now
        extra_feedback = load_feedback_for_window(end_dt, extended_end)
        if extra_feedback:
            print(
                f"[Daily Review] 📎 延伸涵蓋 {end_dt.strftime('%H:%M')} ～ now，"
                f"額外補入 {len(extra_feedback)} 筆 feedback",
                flush=True,
            )
            feedback_records = feedback_records + extra_feedback
    else:
        extended_end = end_dt

    memory = load_memory()

    print(
        f"[Daily Review] STT={len(body)} 行 | Feedback={len(feedback_records)} 筆 | "
        f"Memory players={len(memory.get('players', {}))}",
        flush=True,
    )

    # 4a. 話題標記統計 + 回應速度統計（離線，不需 LLM）
    today_label = now.strftime("%Y-%m-%d")

    print("[Daily Review] 🏷  正在進行話題關鍵字標記...", flush=True)
    topic_stats = compute_topic_distribution(stt_text)
    topic_summary_str = print_topic_summary(topic_stats)

    latency_stats = compute_latency_stats(feedback_records)
    latency_summary_str = print_latency_summary(latency_stats)

    length_stats = compute_response_length_stats(feedback_records)
    print_length_summary(length_stats)

    proactive_stats = compute_proactive_stats(feedback_records, start_dt, now)
    proactive_summary_str = print_proactive_summary(proactive_stats)

    # 寫出話題統計檔
    topic_stats_path = LOG_DIR / f"topic_stats_{today_label}.json"
    try:
        with open(topic_stats_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date":           today_label,
                    "generated":      now.isoformat(),
                    "log_range":      f"{start_dt} ~ {end_dt}",
                    **{k: v for k, v in topic_stats.items() if k != "ranked"},
                    "ranked":         topic_stats.get("ranked", []),
                    "latency_stats":  latency_stats,
                },
                f, ensure_ascii=False, indent=2,
            )
        print(f"[Daily Review] 💾 話題統計 → {topic_stats_path.name}", flush=True)
    except Exception as e:
        print(f"[Daily Review] ⚠ 寫出話題統計失敗: {e}", flush=True)

    # 4b. 組合 user content
    topic_section = ""
    if topic_stats.get("total", 0) > 0:
        # 附上 atmosphere 關聯：哪些 feedback 當下的氣氛快照
        atm_samples = [
            {"speaker": r["speaker"], "reaction": r["reaction_type"],
             "atmosphere": r.get("atmosphere"), "latency": r.get("wake_latency_sec")}
            for r in feedback_records
            if r.get("atmosphere") or r.get("wake_latency_sec") is not None
        ][:30]
        topic_section = (
            f"### D. 話題關鍵字標記統計（AtmosphereTracker 離線分析）\n"
            f"{topic_summary_str}\n"
            f"詳細 per-speaker：\n"
            f"{json.dumps(topic_stats.get('by_speaker', {}), ensure_ascii=False, indent=2)}\n\n"
            f"回應速度：{latency_summary_str}\n\n"
            f"主動發言效益：\n{proactive_summary_str}\n"
            f"詳細 per-topic：\n"
            f"{json.dumps(proactive_stats.get('topics', {}), ensure_ascii=False, indent=2)}\n\n"
            f"氣氛快照 ×feedback 交叉樣本（最多30筆）：\n"
            f"{json.dumps(atm_samples, ensure_ascii=False, indent=2)}\n"
            f"（請根據以上資料輸出 atmosphere_calibration，"
            f"指出現有關鍵字未捕捉到的話題詞彙，以及速度表現評估。"
            f"並在產生 proactive_topics 時優先保留效益≥6 的話題，"
            f"淘汰效益≤2 的話題。）"
        )

    # 只把「今日有出現」的玩家記憶送給 Gemini，減少 input+output token 數
    _active_speakers = set(topic_stats.get("by_speaker", {}).keys())
    _active_speakers |= {r.get("speaker", "") for r in feedback_records}
    _active_speakers.discard("")
    _all_players = memory.get("players", {})
    _active_memory = dict(memory)
    if _active_speakers:
        _active_memory["players"] = {
            k: v for k, v in _all_players.items() if k in _active_speakers
        }
        _skipped = len(_all_players) - len(_active_memory["players"])
        if _skipped:
            print(
                f"[Daily Review] 🗂  記憶精簡：送出 {len(_active_memory['players'])} 位玩家"
                f"（略過 {_skipped} 位今日未出現）",
                flush=True,
            )

    user_content = (
        f"### A. stt_history.log（切片區間：{start_dt} ～ {end_dt}）\n"
        f"{stt_text}\n\n"
        f"### B. response_feedback.jsonl（涵蓋至 {extended_end.strftime('%H:%M')}）\n"
        f"{json.dumps(feedback_records, ensure_ascii=False, indent=2)}\n\n"
        f"### C. suki_memory.json（現有記憶，今日活躍玩家）\n"
        f"{json.dumps(_active_memory, ensure_ascii=False, indent=2)}"
        + (f"\n\n{topic_section}" if topic_section else "")
    )

    # 5. 呼叫 Gemini
    print("[Daily Review] 🤖 送出 LLM bus（analyze tier，多 provider fallback）分析中...", flush=True)
    try:
        result = call_review_llm(user_content)
    except Exception as e:
        print(f"[Daily Review] ❌ LLM bus 分析失敗: {e}", flush=True)
        notify_discord_review(date=args.date or today_label,
                              score=None, trend=None, problem_patterns=[],
                              success=False, error_msg=f"LLM bus 分析失敗: {e}")
        sys.exit(1)

    print("[Daily Review] ✅ Gemini 分析完成，開始合併記憶...", flush=True)

    # 6. 備份
    backup_path = backup_memory()
    if backup_path:
        print(f"[Daily Review] 💾 備份: {backup_path.name}", flush=True)

    # 7. 合併玩家資料（per-player 隔離，一個炸不拖垮其他）
    updated_players  = result.get("players", {})
    existing_players = memory.get("players", {})
    merged_players   = merge_players_safe(existing_players, updated_players)

    # 8. 組合最終記憶
    final_memory = dict(memory)
    final_memory["players"] = merged_players
    for key in ("proactive_topics", "marvin_performance", "wake_analysis",
                "system_suggestions", "_meta"):
        if key in result:
            final_memory[key] = result[key]

    # 8a-ext. 把回應長度建議與主動發言效益存進 marvin_performance
    mp = final_memory.setdefault("marvin_performance", {})
    _today_str = datetime.now().strftime("%Y-%m-%d")  # 8a-liked 與 8b 共用，須先於兩者定義

    # 8a-liked. 離線萃取「喜歡」回應的模式，作為 prompt 改善線索
    _liked_records = [r for r in feedback_records if r.get("reaction_type") == "喜歡"]
    if _liked_records:
        _MEMORY_KWS = ["記得", "你說過", "你曾", "你的", "你最近", "你那", "落枕", "本機模型", "排班", "印表機"]
        _PERSONAL_COUNT = sum(
            1 for r in _liked_records
            if any(kw in r.get("bot_response", "") for kw in _MEMORY_KWS)
        )
        _liked_lengths = [len(r.get("bot_response", "")) for r in _liked_records]
        _avg_liked_len = round(sum(_liked_lengths) / len(_liked_lengths)) if _liked_lengths else 0
        mp["liked_patterns"] = {
            "total_liked":          len(_liked_records),
            "with_personal_memory": _PERSONAL_COUNT,
            "personal_memory_rate": round(_PERSONAL_COUNT / len(_liked_records), 2),
            "avg_response_length":  _avg_liked_len,
            "last_updated":         _today_str,
        }
        print(
            f"[Daily Review] 💚 喜歡模式：{len(_liked_records)} 筆  "
            f"含個人記憶引用={_PERSONAL_COUNT}/{len(_liked_records)}  "
            f"平均長度={_avg_liked_len}字",
            flush=True,
        )
    if length_stats.get("optimal_length"):
        mp["optimal_response_length"] = length_stats["optimal_length"]
        mp["response_length_stats"]   = length_stats["by_reaction"]
        print(f"[Daily Review] 📏 最佳回應長度更新：{length_stats['optimal_length']} 字", flush=True)
    if proactive_stats.get("total_fires", 0) > 0:
        mp["proactive_stats"] = {
            "total_fires": proactive_stats["total_fires"],
            "topics": {
                tid: {"title": td["title"], "fires": td["fires"], "effectiveness": td["effectiveness"]}
                for tid, td in proactive_stats["topics"].items()
            },
        }
        print(
            f"[Daily Review] 💬 主動發言統計更新：{proactive_stats['total_fires']} 次觸發",
            flush=True,
        )

    # 8b. 性格突變 3.0 Phase 1 — 從今日 feedback 萃取 per-player 反應計數
    # 延遲 單獨計數，不納入互動評分分母
    _REACTION_TYPES = ("喜歡", "嚴重", "錯誤", "提出興趣", "延遲")
    _today_counts: dict[str, dict] = {}
    for _rec in feedback_records:
        _spk = _rec.get("speaker", "").strip()
        _rt  = _rec.get("reaction_type", "").strip()
        if not _spk or _rt not in _REACTION_TYPES:
            continue
        if _spk not in _today_counts:
            _today_counts[_spk] = {r: 0 for r in _REACTION_TYPES}
        _today_counts[_spk][_rt] += 1

    if _today_counts:
        existing_pr = final_memory.get("player_reactions", {})
        for _spk, _counts in _today_counts.items():
            _entry = existing_pr.get(_spk, {r: 0 for r in _REACTION_TYPES})
            for _rt in _REACTION_TYPES:
                _entry[_rt] = _entry.get(_rt, 0) + _counts.get(_rt, 0)
            _entry["last_updated"] = _today_str
            existing_pr[_spk] = _entry
        final_memory["player_reactions"] = existing_pr
        print(
            f"[Daily Review] 🧬 player_reactions 更新: {list(_today_counts.keys())}",
            flush=True,
        )

    # 8c-1. 更新 atmosphere_calibration（Gemini 建議的關鍵字補充）
    _atm_calib = result.get("atmosphere_calibration")
    if _atm_calib and isinstance(_atm_calib, dict):
        existing_calib = final_memory.get("atmosphere_calibration", {})
        # 合併 suggested_additions：union 不重複
        new_adds = _atm_calib.get("suggested_additions", {})
        merged_adds = dict(existing_calib.get("suggested_additions", {}))
        for topic, kws in new_adds.items():
            old_kws = merged_adds.get(topic, [])
            merged_adds[topic] = list(dict.fromkeys(old_kws + [k for k in kws if k not in old_kws]))
        existing_calib["suggested_additions"] = merged_adds
        existing_calib["accuracy_note"]       = _atm_calib.get("accuracy_note", "")
        existing_calib["response_speed_note"] = _atm_calib.get("response_speed_note", "")
        existing_calib["last_updated"]        = today_label
        existing_calib["latency_stats"]       = latency_stats
        final_memory["atmosphere_calibration"] = existing_calib
        print(
            f"[Daily Review] 🌡  atmosphere_calibration 更新："
            f" 話題補充={list(merged_adds.keys())}",
            flush=True,
        )

    # 8c-2. 把今日話題統計寫入 final_memory（最近 7 天 rolling）
    if topic_stats.get("total", 0) > 0:
        hist = final_memory.get("daily_topic_stats", {})
        hist[today_label] = {
            "total":      topic_stats["total"],
            "hit_rate":   topic_stats["hit_rate"],
            "topic_total": topic_stats["topic_total"],
            "speaker_top": topic_stats.get("speaker_top", {}),
        }
        # 只保留最近 7 天
        if len(hist) > 7:
            for old_key in sorted(hist.keys())[:-7]:
                del hist[old_key]
        final_memory["daily_topic_stats"] = hist

    # 8d. 強制保證 _meta.review_date 推進（Gemini 漏 _meta 也不該 silent 失敗）
    _enforce_meta_review_date(final_memory, args.date or today_label)

    # 9. 備份舊記憶 + 寫回
    today_str = datetime.now().strftime("%Y-%m-%d")
    bak_path = MEMORY_FILE.parent / f"suki_memory.{today_str}.bak"
    if MEMORY_FILE.exists() and not bak_path.exists():
        shutil.copy2(MEMORY_FILE, bak_path)
        # 清除超過 7 天的備份（保留至少 2 份）
        bak_files = sorted(MEMORY_FILE.parent.glob("suki_memory.*.bak"))
        if len(bak_files) > 7:
            for old_bak in bak_files[:-7]:
                old_bak.unlink(missing_ok=True)
        print(f"[Daily Review] 💾 備份至 {bak_path.name}", flush=True)

    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(final_memory, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # 9b. player 寫回 SQLite（bot 權威來源）——只寫本輪 Gemini 實際更新者。
    # 順序在 json 寫出之後：replace_player_memory 的 _export_json 會保留上面剛寫的 meta，
    # 並把 json player 區段同步成 db 的 repaired 版本，讓 db / json 最終一致。
    try:
        synced = persist_players_to_db(
            merged_players, list(updated_players.keys()),
            db_path=str(BASE_DIR / "marvin.db"), json_path=str(MEMORY_FILE),
        )
        print(f"[Daily Review] 🗄  player 寫回 SQLite：{synced} 位", flush=True)
    except Exception as e:
        print(f"[Daily Review] ⚠ player 寫回 SQLite 失敗（json 已寫，bot 重啟前不生效）: {e}",
              flush=True)

    score = result.get("marvin_performance", {}).get("score", "N/A")
    trend = result.get("marvin_performance", {}).get("trend", "")
    print(
        f"[Daily Review] 🎉 suki_memory.json 更新完成。"
        f"今日分數: {score}  趨勢: {trend}",
        flush=True,
    )

    notify_discord_review(
        date=args.date or today_label,
        score=score if isinstance(score, (int, float)) else None,
        trend=trend or None,
        problem_patterns=result.get("marvin_performance", {}).get("problem_patterns", []),
        success=True,
    )

    # 10. STT 音近字修正表聚合
    build_stt_corrections_dict()

    # 11. 喚醒詞建議自動應用
    _wake_analysis       = result.get("wake_analysis", {})
    _suggested_additions = _wake_analysis.get("suggested_additions", [])
    _suggested_removals  = _wake_analysis.get("suggested_removals", [])
    if _suggested_additions or _suggested_removals:
        _override_path = BASE_DIR / "records" / "wake_words_override.json"
        try:
            _existing = (
                json.loads(_override_path.read_text(encoding="utf-8"))
                if _override_path.exists()
                else {}
            )
            _curr_additions = _existing.get("additions", [])
            _curr_removals  = _existing.get("removals", [])
            for w in _suggested_additions:
                if w and w not in _curr_additions:
                    _curr_additions.append(w)
            for w in _suggested_removals:
                if w and w not in _curr_removals:
                    _curr_removals.append(w)
            _override_path.write_text(
                json.dumps(
                    {"_updated": today_str, "additions": _curr_additions, "removals": _curr_removals},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            print(
                f"[Daily Review] 🔤 wake_words_override.json 更新："
                f" +{len(_suggested_additions)} 新增  -{len(_suggested_removals)} 移除",
                flush=True,
            )
        except Exception as e:
            print(f"[Daily Review] ⚠ 寫出 wake_words_override.json 失敗: {e}", flush=True)


if __name__ == "__main__":
    main()
