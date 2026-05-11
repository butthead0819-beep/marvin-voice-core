
## TDD 開發模式（預設行為）

實作任何新功能或修 bug 時，**永遠先寫測試，再寫實作**。不需要用戶提醒。

### 流程

1. **寫失敗測試**：用 `tests/test_<feature>.py` 描述預期行為（assert 什麼、回傳什麼、狀態怎麼變）
2. **確認全紅**：執行 `pytest tests/test_<feature>.py`，確認所有測試都失敗（這證明測試有意義）
3. **寫最小實作**：只寫讓測試通過所需的程式碼，不多也不少
4. **確認全綠**：執行 pytest，全部通過才算完成
5. **Commit**：測試與實作放同一個 commit

### 測試命名原則

- `test_<行為描述>_<預期結果>`，例如 `test_select_theme_rejects_unknown_theme`
- 每個測試只驗證一件事
- Fallback / edge case 一定要有對應測試

### 這個專案的測試慣例

- 使用 `pytest` + `pytest-asyncio`
- Discord 相關（bot、cog）用 `MagicMock` + `AsyncMock`，`bot.cogs.get.return_value = None` 關掉 VoiceController
- DB 操作用 `db_path=":memory:"`
- 不測 Discord embed 格式，只測狀態機行為與回傳值

---

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
