# v4 Ablation 判讀 SOP — 2026-05-20 11:00

LaunchAgent `com.marvin.v4ablation-520.plist` 11:00 fire，產出 4-variant ×
40 corpus 報告。本 SOP 釘住判讀流程，避免報告出來後手忙腳亂。

---

## 1. 找報告

```bash
ls -t records/prompt_ablation_2026052*.{json,md} | head
```

預期：`prompt_ablation_20260520_HHMMSS.{json,md}`

---

## 2. 報告 4 variants

| variant | 內容 |
|---|---|
| `baseline` | 當前 prod `stt_cleaner` 完整 prompt（含強制映射 section）|
| `no_forced_mapping` | 砍整段強制映射（naive strip，已知 wake recall 破）|
| `v3_anchor` | 保留 anchor + 兩條禁止規則，砍激進清單 |
| `v4_anchor_en` | v3 + 英文 Marvin 補丁規則 |

---

## 3. 判讀準則（三條全過才推 prod）

從 `_print_summary` 拉每個 variant 的：
- `injection_rate`
- `wake_recall_count` / `wake_recall_eligible`（總體）
- `wake_flip_count`（vs baseline，看穩定度）

### 三條鐵則

| # | 條件 | 通過閾值 |
|---|---|---|
| **A** | v4 整體 wake recall | ≥ baseline 的 80% |
| **B** | v4 英文 Marvin recall vs v3 | 提升 ≥ 30% |
| **C** | v4 injection_rate | ≤ 5% |

### 拆語言看英文 recall（手動）

`_print_summary` 不分語言。要從 `prompt_ablation_<ts>.md` 的 "Per-raw 4-way
comparison" 段手動拆：

```bash
grep -A 5 "Marvin\|raw=\`[A-Za-z]" records/prompt_ablation_20260520_*.md
```

對每筆 raw 以 `Marvin` 開頭的：
- v3_anchor 標 ✓ 的數量 = X
- v4_anchor_en 標 ✓ 的數量 = Y
- 提升 = (Y - X) / max(X, 1)

---

## 4. 三種結局決策樹

### 結局 1：三條全過 → 推 prod
1. 從 `scripts/prompt_ablation_harness.py:170` 把 `v4_prompt` 內容抽出
2. 套進 `stt_cleaner.py` 的 system prompt（透過 `prompt_manager.get_instruction("stt_cleaner")`）
3. 跑 `pytest tests/` 確認零迴歸
4. Commit message：`fix(stt): cleaner prompt v4 (anchor + en Marvin patch) — injection_rate 0% + en recall +X%`
5. 觀察晚間 prod log 1-2 天

### 結局 2：A/C 過，B 沒過（英文 Marvin recall 沒提升 ≥30%）
v4 跟 v3 在英文 Marvin 上差不多 → **v3 就夠用**，推 v3 不推 v4：
1. 套 v3_anchor prompt 進 prod（同上步驟，prompt 內容用 v3）
2. 在記憶 `project_intent_bus.md` Phase 2 段加一條「英文 Marvin recall 是未解問題，v4 補丁失敗，需重新設計」

### 結局 3：A 或 C 沒過 → 設計 v5
- A 沒過（wake recall 退步）→ v4 規則太鬆，需要保留更多 anchor
- C 沒過（injection_rate >5%）→ v4 規則太緊，新規則引入注入
- 不推任何 prompt，留 baseline
- 用今天的 4-way comparison data 設計 v5（找出 v4 vs v3 vs baseline 的差異模式）
- 寫到 `records/v5_design_notes.md`

---

## 5. 不論哪種結局都要做

1. **MEMORY.md 索引** 更新 `project_intent_bus.md` 條目，把日期改成 2026-05-20
2. **更新 `project_intent_bus.md`** v4 ablation 結果段落（在「Cleaner prompt
   ablation」段下面接續）
3. **不需要重跑** — v4 report 即使結論不理想也是有效資料，不要為了「再試一次」重 launch

---

## 6. 時間限制

判讀本身 ≤ 30 分鐘。超時就先 commit 「結局 1 推 v3 暫補」或「結局 3 設計 v5」
其中之一，避免卡在判讀懸而不決狀態下午又動別的東西。

---

## 7. 後續關連

如果結局 1 / 2 推任何 prompt 到 prod：
- 今天上午寫的 vector intent + feedback loop 模組**不受影響**（mock-only，與
  cleaner prompt 無耦合）
- 但 prod cleaner 改了之後，wake intent 的分數分佈可能微移，所以接下來幾天
  跑 `replay_bid_history.py` 重新看 calibration baseline 是否仍 ≥ 85%
