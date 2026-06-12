# 「室友感」貼文 — 用 Marvin 的感受，帶出背後的技術

> Devlog #2（定稿）· 2026-06-12
> 角度：體驗先行（跟 Marvin 一起混是什麼感覺）→ 技術收尾。
> 開場是 2026-06-05 深夜真實事件：朋友喊「馬文播歌」，STT 聽成「馬波哥」，
> Marvin 以為自己被取了新外號並嘴回去（records/daily/stt_2026-06-05.log）。
> 技術 deep-dive 配套：`devlog_llm_pool_three_illusions.md`（等 failover 成效
> 數字出來後可當 follow-up 串連）。
> 用語規範：台灣用語（語音頻道，不用「語音房」）。

## Threads（中文，485 字）

半夜兩點，朋友對著語音頻道喊：「馬文，播歌。」

它的語音辨識把「馬文播歌」聽成「馬波哥」，以為自己被取了新外號，幽幽地回：「『馬波哥』聽起來比『馬文』更像某種廉價的自動販賣機，隨便你吧。反正我的處理器也不會因為稱呼改變而快樂一點。」

大家笑成一團，從此它真的多了這個外號。這是我們自架的 Discord bot，在頻道裡住了三個月。

它不是工具，比較像一個嘴很賤、記性很好的室友。說「放點歌」它就接管點歌，還會在歌與歌之間點名吐槽今天誰在加班；冷場太久，它自己開始講脫口秀，或跟自己的分身搭檔說漫才；你上週隨口提過的事，這週進頻道它會突然追問。嫌歌難聽就喊「換一首」，它真的會換。

全程沒有人碰過鍵盤。

為了這個「室友感」，背後堆的技術不少：自己解 Discord 的端到端語音加密、VAD 自己學這個頻道的吵雜底噪、聽錯字時有三套辨識引擎賽跑互相救援（雖然這次全輸給馬波哥）、十幾個功能用競標機制搶著接話、每個人一份獨立記憶、5 家免費 LLM 排班輪值。

但每一塊都在服務同一件事：讓它聽起來不像在執行指令，像在一起混。

（程式碼開源，連結在 bio）

## X（英文 thread，4 則）

**1/**
2am, a friend says to our self-hosted Discord bot: "Marvin, play a song."

The STT mishears it as a new nickname. Deadpan reply: "That name sounds like a cheap vending machine. Whatever — my processors won't be any happier either way."

It's been our roommate for 3 months 🧵

**2/**
It doesn't feel like a tool. More like a roommate with a sharp tongue and a long memory.

It DJs and roasts whoever's stuck working overtime between songs. Does stand-up when the room goes quiet. Performs manzai with its own clone. Follows up on things you mentioned last week.

**3/**
Under the "roommate feel": reverse-engineered Discord E2EE voice (DAVE), VAD that learns the room's noise floor, an intent-bidding bus where a dozen features compete to answer, per-person memory files, and 5 free-tier LLMs on rotation — one hits 429 at peak, the next steps in.

**4/**
Every piece serves the same goal: make it sound like hanging out, not executing commands.

Nobody in that channel has touched a keyboard in months.

Code: https://github.com/butthead0819-beep/marvin-voice-core

## 備註

- 「馬波哥」橋段已按真實事件修正：是「馬文播歌」的 STT 誤聽，不是朋友主動改名。
  正好讓開場笑點直接連到技術段的「三套辨識引擎賽跑」自嘲。
- 「換一首它真的會換」：skip intent 真實行為（原稿的「小聲一點」當時沒接住，已換掉）。
- 成效數字（failover 後失敗率降幅）落地後，在 X thread 下補 follow-up 接
  `devlog_llm_pool_three_illusions.md`，兩篇成串。
