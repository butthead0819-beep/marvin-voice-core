# Streamer Quickstart：5 分鐘把 Marvin 帶進你的 Discord

> 給實況主的最短路徑。不需要 coding 經驗，會複製貼上就能裝。
>
> 卡住任何一步請 DM 我 — 我陪你弄好，15 分鐘搞定。

---

## 你需要什麼

| 項目 | 為什麼 |
|---|---|
| **Mac**（M1 / M2 / M3 / M4 都可，8GB+） | Marvin 用 macOS 內建的 Swift 語音辨識，免費又快 |
| **15 分鐘** | 一次設定完，之後永遠不用再碰 |
| **3 個免費帳號** | Discord 開發者、Groq、Google AI Studio — 都有免費額度，一般使用一個月 $0 |

不需要：信用卡、Docker、會寫 Python。

---

## Step 1：申請 Discord Bot（5 分鐘）

1. 開 [Discord Developer Portal](https://discord.com/developers/applications)
2. 右上角 **New Application** → 隨便取名字（例如 "我的 Marvin"）→ Create
3. 左側選 **Bot** → 右邊往下捲到 **Privileged Gateway Intents**，**三個全部打開**：
   - PRESENCE INTENT
   - SERVER MEMBERS INTENT
   - MESSAGE CONTENT INTENT
4. 同一頁最上面 **Reset Token** → 複製出來的 token（很長一串）**先存到記事本**，等下要用
5. 左側選 **OAuth2** → **URL Generator**：
   - **SCOPES** 勾：`bot` + `applications.commands`
   - **BOT PERMISSIONS** 勾：`Send Messages`、`Connect`、`Speak`、`Use Voice Activity`、`Read Message History`
   - 最下面會跑出一條 URL — 複製，貼到瀏覽器 → 選你要邀請的 server → Authorize

✅ 完成的話，Discord 你的 server 應該會看到 bot 已加入（離線狀態）。

---

## Step 2：拿 Groq API Key（2 分鐘）

Groq 跑 Marvin 的語音清洗 + 備援 LLM，**免費 tier 一般單人用一個月用不完**。

1. 開 [console.groq.com](https://console.groq.com/) → 用 Google 或 GitHub 登入
2. 左側 **API Keys** → **Create API Key** → 複製存到記事本

---

## Step 3：拿 Gemini API Key（2 分鐘）

Gemini 是 Marvin 的主要對話腦袋。**免費 tier 每天 1500 次請求，一般直播聊天用不完**。

1. 開 [Google AI Studio](https://aistudio.google.com/) → 用 Google 登入
2. 點 **Get API key** → **Create API key** → 複製存到記事本

---

## Step 4：跑安裝指令（5 分鐘）

打開 **Terminal**（按 `Cmd + Space` 打 "Terminal" → Enter），複製貼上：

```bash
curl -fsSL https://raw.githubusercontent.com/butthead0819-beep/marvin-voice-core/main/install-marvin.sh | bash
```

這個指令會：
1. 自動裝 Homebrew（如果你還沒裝，會問你輸密碼）
2. 自動裝 Python 3.12 + git
3. 下載 Marvin 到 `~/marvin`
4. 安裝 Python 套件
5. 互動式問你 3 個 API key，自動寫進 `.env`

跑到一半會停下來問你貼 token / key — 把 Step 1-3 存的三個複製貼上即可。

安裝完成後，腳本會印出最後啟動指令：

```bash
cd ~/marvin && python3.12 main_discord.py
```

✅ 看到 `Marvin 上線` 或 `Logged in as ...` 就成功了。Discord 你的 bot 會從離線變綠燈。

---

## Step 5：召喚 Marvin

1. 加入你 server 的任何一個語音頻道
2. 在文字頻道打 `/summon`
3. Marvin 會進來那個語音頻道，講第一句話跟你打招呼

---

## 直播時的注意事項

- **保持 Mac 開機**：Marvin 跑在你的電腦上，Mac 睡眠 = Marvin 離線
- **直播軟體別把 Marvin 的 TTS 收音兩次**：OBS 用「應用程式音訊」單獨抓 Discord 即可
- **第一次測試建議找 2-3 個朋友**：Marvin 越多人講話、越多次互動，個性會越鮮明（他會記得每個人的習慣、口味、玩過的歌）
- **/marvin_optin** / **/marvin_optout**：每個觀眾首次加入語音頻道，Marvin 會私訊問是否同意被處理語音。沒同意的人完全不會被聽到

---

## 我設定卡住了怎麼辦

<!-- TODO(jack): 把下方 YOUR_DISCORD_HANDLE 換成你的 Discord username（不是 display name） -->
**DM 我**（Discord：`jackhuang0819`），我陪你 Zoom 15 分鐘弄好。

最常見三個卡點：
1. **Discord bot 加入了但沒上線** → 八成是 Step 1 的三個 Intents 沒打開
2. **`pip3 install` 報錯** → 通常是 Mac 沒裝 Xcode Command Line Tools，跑 `xcode-select --install`
3. **bot 上線了但 `/summon` 沒反應** → bot 在 Discord server 沒看到 slash command；等 1-2 分鐘 Discord 同步，或重啟 bot

---

## 之後怎麼維護

設定好後，Marvin 會：
- 自動記憶每個人（在你 Mac 上的 SQLite 檔，從不外傳）
- 對每個人講話風格不一樣（depressed 哲學家版的 Marvin，但會看人下菜）
- 知道你在玩什麼遊戲、聊什麼話題（real-time topic tracking）
- 推薦音樂時看「這個 room 喜歡什麼」，不是 generic genre

要關掉：Terminal `Ctrl+C` 中止 `python3 main_discord.py`。

要重新跑：Terminal `cd ~/marvin && python3 main_discord.py`。

要永久後台跑（Mac 重開機也自動啟動）：DM 我，我給你 LaunchAgent 設定檔。

---

## 隱私聲明

- **語音資料**：經 Mac 本地 Swift STT 轉文字 → Groq 清洗錯字 → Gemini 回應
- **記憶**：存在你 Mac 上的 SQLite (`marvin.db`)，不上雲、不傳給維護者
- **第三方 API**：Groq + Gemini 收到的是「清洗過的對話文字」，他們的 free tier 條款適用
- **觀眾同意**：第一次加入語音頻道會被問是否同意，沒同意完全不處理

---

**這份 guide 還沒寫完？覺得哪一步說不夠清楚？** 直接 DM 我，我會根據你卡的地方持續改善這份文件。
