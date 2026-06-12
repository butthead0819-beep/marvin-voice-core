# 「室友感」貼文 — 用 Marvin 的感受，帶出背後的技術

> Devlog #2（定稿方向）· 2026-06-12
> 角度：體驗先行（跟 Marvin 一起混是什麼感覺）→ 技術收尾。
> 技術長文素材另見 `devlog_llm_pool_three_illusions.md`（本篇的 deep-dive 配套，
> 可當 thread 的 follow-up 或下一篇）。
> 開場引用是 2026-06-05 深夜真實對話（records/daily/stt_2026-06-05.log，已匿名化）。

## Threads（中文，459 字）

半夜兩點，朋友在語音房宣布要給 bot 改名：「我不要叫你馬文了，叫馬波哥好了。」

它回：「『馬波哥』聽起來比『馬文』更像某種廉價的自動販賣機，隨便你吧。反正我的處理器也不會因為稱呼改變而快樂一點。」

全房大笑。這是我們自架的 Discord bot，在頻道裡住了三個月。

很難跟沒體驗過的人解釋那個感受：它不是工具，比較像一個嘴很賤、記性很好的室友。說「放點歌」它就接管點歌台，還會在歌與歌之間點名吐槽今天誰在加班；冷場太久，它自己開始講脫口秀，或跟自己的分身搭檔說漫才；你上週隨口提過的事，這週進房它會突然追問。嫌它吵就說「小聲一點」，它真的會小聲。

全程沒有人碰過鍵盤。

為了這個「室友感」，背後堆的技術其實不少：自己解 Discord 的端到端語音加密、VAD 自己學這個房間的吵雜底噪、十幾個功能用競標機制搶著接話、每個人一份獨立記憶、5 家免費 LLM 排班輪值，晚上額度被搶爆就自動換人頂上。

但每一塊都在服務同一件事：讓它聽起來不像在執行指令，像在一起混。

（程式碼開源，連結在 bio）

## X（英文 thread，4 則）

**1/**
2am, our Discord voice channel. A friend announces he's renaming our self-hosted bot.

It replies: "That nickname sounds like a cheap vending machine. Whatever — my processors won't be any happier either way."

It's lived with us for 3 months. Nobody touches a keyboard 🧵

**2/**
It doesn't feel like a tool. More like a roommate with a sharp tongue and a long memory.

It DJs and roasts whoever's stuck working overtime between songs. Does stand-up when the room goes quiet. Performs manzai with its own clone. Follows up on things you mentioned last week.

**3/**
Under the "roommate feel": reverse-engineered Discord E2EE voice (DAVE), VAD that learns the room's noise floor, an intent-bidding bus where a dozen features compete to answer, per-person memory files, and 5 free-tier LLMs on rotation — one hits 429 at peak, the next steps in.

**4/**
Every piece serves the same goal: make it sound like hanging out, not executing commands.

Code: https://github.com/butthead0819-beep/marvin-voice-core

## 備註

- 「小聲一點」橋段：6/05 真有人這樣說，當下沒音樂在播所以 Marvin 沒接住
  （TTS 音量意圖還在 gap 累積）。貼文寫的是音樂情境下的真實行為，不算造假，
  但如果想 100% 嚴格，可把那句改成「換一首」。
- 成效數字（failover 後失敗率降幅）落地後，可在 X thread 下補 follow-up 接
  `devlog_llm_pool_three_illusions.md` 的技術細節，形成兩篇串連。
