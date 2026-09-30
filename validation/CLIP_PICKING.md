# 推廣素材挑片規則（給 Claude Code，每次挑片照做）

> 2026-09-27 定案。流程：錄音 → 時間軸 → **Claude 挑候選** → Jack 看文字審閱 → 批次出片 → 交給 Cowork。
> 設定錄音見 `RECORDING_SETUP.md`。

## 流程與指令

```
# 1. 產生時間軸（使用者逐字稿＋馬文台詞＋罐頭回應，錄音內相對時間）
./venv_simon/bin/python -m scripts.clip_timeline timeline "<錄音>.mkv"
#    → <錄音>.timeline.txt（Claude 讀這份）＋ .timeline.json

# 2. Claude 讀 timeline.txt，挑候選寫成 <錄音>.candidates.json（格式見下方）
./venv_simon/bin/python -m scripts.clip_timeline validate "<錄音>.candidates.json"
./venv_simon/bin/python -m scripts.clip_timeline review   "<錄音>.candidates.json"
#    → <錄音>.candidates.md 給 Jack 讀；Jack 回覆要用的編號與要改的字

# 3. 把 Jack 通過的 clip 設成 "approved": true，出片
./venv_simon/bin/python -m scripts.clip_render render "<錄音>.candidates.json"
#    → ~/Documents/Marvin-Validation/素材/<id>.mp4 ＋ index.md（Cowork 從這裡挑）
```

bot 頭像已抓好（`assets/marvin_avatar.png`）；bot 換頭像後重跑 `./venv_simon/bin/python -m scripts.clip_render fetch-avatar`。所有指令都要在 repo 根目錄執行。

## 要挑的（只取閒聊）

- 馬文吐槽有笑點：有人講了一句，馬文接話，接著有人有反應（笑、回嘴）
- **DJ 口白裡的選歌理由與吐槽**：這是最差異化的素材，優先挑
- 冷場時拿剛剛的話題丟話題、「你之前說要…現在呢？」
- 進場招呼、送客（有完整前後文的才挑）
- 長度 15–60 秒；前面要有能看懂的鋪陳，結尾停在笑點或反應之後
- 每份錄音最多挑 8 段，寧缺勿濫

## 整段不用（任何一條命中就跳過，不要想辦法救）

1. **人講的原聲**裡出現：真名或別人的暱稱、地址或比「縣市」更細的地點、公司／學校、健康、家人、感情、錢、遊戲 ID、帳號、電話
2. 提到**不在場的第三者**的事
3. 片段裡有**沒答應被剪成短片的人**在講話（錄音是混成一軌，無法只拿掉一個人）
4. 爭吵、有人明顯不開心、政治、宗教、成人內容
5. `marvin_audio` 是 `"original"`（錄音裡有馬文），而該段在放歌

## 可以修改的地方

- **人的字幕**：修正 STT 聽錯的字，讓字幕等於實際講的話。不可改變意思、不可加字
- **馬文的台詞**（`marvin` 事件）：他的聲音會重新合成，所以提到名字或個資的地方可以改寫（例如把名字換成「某位朋友」）。改寫後意思與笑點要保留，**不可編出他沒講過的笑點**
- 罐頭回應（`ack`）不改
- DJ 片段：在歌開始的位置加 `song_card`（歌名、歌手），不放歌。2026-09-28 06:41 之後的錄音，時間軸已有 `歌曲 🎵《…》` 行（timeline.json 裡是現成的 song_card 事件），直接複製；歌名是 YouTube 原標題，要清成乾淨歌名並補歌手。之前的錄音要去 `bot_main.log` 找 `[Stream Loop] 播放:` 自己對時間

## 候選檔格式（candidates.json）

- `marvin_audio`：錄音帳號有把馬文本地靜音 → `"resynth"`；第一場（Jack 本人用 Mac、停掉音樂）→ `"original"`
- `marvin_delay`：預設 0.3 秒；出片後聽起來馬文太早或太晚再調
- 每段：`id`（c01…）、`approved: false`、`title`（一句話描述笑點）、`post_type`（名場面／功能／招募）、`from`、`to`、`note`（為什麼好笑、有沒有什麼疑慮）、`events`
- 事件從 timeline.json 複製，再依上面規則修改文字；`human` 的 `to` 是估計值，必要時微調

### 完整欄位定義

所有時間（`from` / `to`）都是**相對錄音開頭的秒數**（float）。

```json
{
  "recording": "/abs/path/2026-09-28 22-10-05.mkv",
  "rec_start": "2026-09-28 22:10:05",
  "marvin_audio": "resynth",
  "marvin_delay": 0.3,
  "clips": [
    {
      "id": "c01",
      "approved": false,
      "title": "馬文吐槽宵夜選擇",
      "post_type": "名場面",
      "from": 123.0,
      "to": 158.0,
      "note": "給審閱者看的說明（選填）",
      "events": [
        {"kind": "human",     "speaker": "狗與露", "from": 124.0, "to": 127.5, "text": "修正後的字幕文字"},
        {"kind": "marvin",    "voice": null,   "from": 128.0, "text": "馬文台詞（可改寫）"},
        {"kind": "ack",       "file": "assets/acks/music/music_ack_03.mp3", "from": 131.0, "text": "這首好聽"},
        {"kind": "song_card", "from": 135.0, "to": 139.0, "title": "七里香", "artist": "周杰倫"}
      ]
    }
  ]
}
```

- `marvin_audio`：`"resynth"`＝錄音裡沒有馬文（錄音帳號把他本地靜音），要把 marvin/ack 疊回去；`"original"`＝錄音裡已經有馬文，不疊。
- `marvin_delay`：marvin/ack 事件實際出聲比紀錄晚的秒數，疊音與顯示字幕時都加上。
- `marvin` 事件的 `voice`：`null`＝馬文；字串＝Marmo 的 edge-tts voice 名稱。沒有 `to`（長度由合成結果決定）。
- `ack` 事件的 `file` 是相對 repo 根目錄的路徑（或絕對路徑）。
- `human` 事件的 `speaker` 是**原始顯示名稱**（輸出影片時一律換成通用頭像，不會出現在影片裡）。
- `song_card`：只在畫面顯示「正在播放」卡片，**不放任何歌曲音訊**；卡片開始時疊一次刷碟音效 `assets/dj_sfx/scratch.wav`。
- 所有事件的 `from` 必須在 `[clip.from, clip.to)` 內。

## 審閱稿給 Jack 時

在 candidates.md 之外，另外用一兩句話說明：挑了幾段、跳過了哪些類型（例如「3 段有個資跳過」）、哪幾段最推薦。
