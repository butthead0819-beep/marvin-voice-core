# DEPENDENCIES — 依賴盤點

> 盤點日 2026-09-27。**以程式碼為準**：AST 掃描全 repo 的 `import` / `from … import`（排除 `venv_simon/`），
> 對照 prod 環境 `venv_simon`（Python 3.13.5）實際安裝版本。
> 「待確認」＝程式碼裡找不到足夠證據，沒有猜。

相關檔案：

| 檔案 | 內容 |
|---|---|
| `requirements.txt` | bot 執行期 + 測試（CI 用同一份），全部鎖版本 |
| `requirements-scripts.txt` | `scripts/` 離線工具才用到的套件（新建） |
| `requirements-core.txt` / `pyproject.toml` | 只裝 `marvin_voice_core/` 語音 pipeline（本次未動） |

驗證方式（皆已跑過、通過）：
- 覆蓋檢查：每個 prod/test 的第三方 import 都對應到 `requirements.txt` 的某個套件；每個鎖定版本都等於 venv 實裝版本。
- 解析檢查：`uv pip compile` 對 macOS arm64（Python 3.12、3.13）與 Linux x86_64（Python 3.12）皆可解出完整依賴樹。

---

## 1. 盤點結果摘要

### 1a. 有被使用、但原本沒列在 requirements.txt

| 套件 | 誰在用 | 處理 |
|---|---|---|
| `chromadb` | `vector_store.py`（**沒有** try/except，缺了 bot 起不來） | 已補進 requirements.txt |
| `PyNaCl` | `discord_voice_engine.py`、Discord voice 加解密 | 已補 |
| `audioop-lts` | discord.py 在 3.13 的替代品（見 §4） | 已補，並加 `python_version >= "3.13"` 標記 |
| `scipy` | `spatial_voice_renderer.py` | 原本有列（沒鎖版本），已鎖版本 |
| `wyoming` | `marvin_voice_core/wyoming_bridge.py`、`wyoming_speaker_output.py` | 原本有列、保留 |
| `websockets` | `yating_stt.py`（直接 import） | 已補（目前只是 google-genai 的間接依賴而已） |
| `PyYAML` / `Pillow` | `persona_loader.py`、`joke_bank.py` / 封面卡片 | 原本有列但沒鎖版本，已鎖 |
| `psutil` | `memory_guard.py` | ⚠️ **prod venv 沒裝** → `is_memory_critical()` 恆回 False，MemoryGuard（RAM ≥92% 時跳過向量庫寫入）**目前實際上是失效的**。裝上會改變行為，所以只在 requirements.txt 留註解，**要不要裝待你決定** |
| `mlx_whisper` | `mlx_whisper_bin.py`（獨立 CLI，bot 不會呼叫） | venv 沒裝，不列 |
| `requests`、`spotipy`、`matplotlib`、`google-api-python-client`、`google-auth-oauthlib`、`onnxruntime` | 只有 `scripts/` | 移到 `requirements-scripts.txt` |
| `onnx`、`tensorflow`、`openwakeword` | 只有 `scripts/`（喚醒詞模型實驗） | venv 沒裝，版本待確認，只在 requirements-scripts.txt 留註解 |
| `alsaaudio`、`dbus`、`gi` | `device/`（跑在 Raspberry Pi 上，不在 Mac） | 不列，屬 Pi 端環境 |

### 1b. 有列在 requirements.txt、但沒被使用

| 套件 | 說明 | 處理 |
|---|---|---|
| `soundfile==0.13.1` | 全 repo 沒有任何 `import soundfile` | 已移除 |

### 1c. 版本漂移（requirements 寫的 ≠ prod 實際跑的）

| 套件 | 原本寫 | prod 實裝 | 處理 |
|---|---|---|---|
| `yt-dlp` | 2026.6.9 | 2026.7.4 | 以實裝為準 |
| `aiohttp` | 同時出現 `>=3.9` 與 `==3.13.4`（舊檔把 core 與全量兩份內容接在一起） | 3.13.4 | 合併成一行 |
| `discord.py` 等 | 前半段 `>=`、後半段 `==` 重複列 | — | 去重、全部 `==` |

---

## 2. Python 套件逐項說明

「可拿掉？」判斷標準：拿掉後 bot 還能不能跑、會失去什麼。有 try/except 包住的 import 缺了會靜默降級，不會 crash。

### Discord / 語音傳輸

| 套件 | 用途 | 使用模組 | 可拿掉？ |
|---|---|---|---|
| `discord.py` | Bot 本體、slash 指令、語音連線 | `main_discord.py`、`cogs/*`、多處 | ❌ 核心 |
| `discord-ext-voice_recv` | **接收**語音頻道音訊（discord.py 原生只會送不會收） | `cogs/voice_controller*.py`、`discord_voice_engine.py`、`marvin_voice_core/sink.py` | ❌ 核心；alpha 版（0.5.2a179），升級要小心 |
| `davey` | DAVE（Discord 端對端加密語音）第二層解密 | `davey_bridge.py`、`discord_voice_engine.py`、`marvin_voice_core/sink.py`（後兩者有 try/except） | ❌ 實務上不可拿；缺了在啟用 DAVE 的頻道會聽不到（STT 全死） |
| `PyNaCl` | Discord voice 傳輸層加解密 | `discord_voice_engine.py` | ❌ |
| `audioop-lts` | 補回 3.13 移除的 `audioop`，給 discord.player / voice_recv 用 | 間接（discord.py 依賴） | ❌ 在 3.13 上不可拿（見 §4） |

### 音訊處理

| 套件 | 用途 | 使用模組 | 可拿掉？ |
|---|---|---|---|
| `numpy` | PCM 運算、VAD、混音 | 22 個 prod 模組 | ❌ |
| `scipy` | 空間化語音渲染（濾波） | `spatial_voice_renderer.py`（被 `local_mixing_source.py`、`voice_controller_playback.py` 使用） | ⚠️ 拿掉會壞混音台路徑；非核心但目前在用 |
| `sounddevice` | 本機麥克風 / 本機喇叭（本機模式、衛星） | `marvin_voice_core/local_mic_sink.py`、`playback_device.py`（有 try/except） | ✅ 純 Discord 用途可拿；本機/衛星模式需要 |
| `lameenc` | MP3 串流編碼（車用 puck / 衛星 `/audio_stream`） | `marvin_voice_core/mp3_stream_encoder.py` | ✅ 純 Discord 可拿；車用 puck 需要 |
| `wyoming` | Pi 書架喇叭（wyoming-satellite 協定） | `marvin_voice_core/wyoming_bridge.py`、`wyoming_speaker_output.py` | ✅ 純 Discord 可拿；衛星需要 |
| `faster-whisper` | 非 Apple 平台的本地 STT 備援 | `discord_voice_engine.py`（`STT_ENGINE` 不是 macos/mlx 才載入） | ✅ macOS prod 不會載入；Linux/Docker 需要。套件很重（ctranslate2） |

### STT / TTS / LLM

| 套件 | 用途 | 使用模組 | 可拿掉？ |
|---|---|---|---|
| `edge-tts` | 主力 TTS（微軟 Edge 線上語音，免費） | `tts_engine.py` | ❌ 拿掉只剩 macOS `say` 備援，音質差很多 |
| `google-genai` | Gemini（主回應備援、音訊救援、情緒分析、`/marvin_talk`、grounded QA） | `gemini_router*.py`、`intent_agents/audio_rescue_*`、`grounded_qa_agent.py`、`marvin_talk.py`、`llm_pool.py` 等 12 個 | ❌ |
| `google-generativeai` | **已停止維護的舊版** Gemini SDK | 只剩 `game_dict_manager.py` | ✅ 可以拿，但要先把 `game_dict_manager.py` 改用 `google-genai` |
| `groq` | Groq（串流回應第一順位、摘要/壓縮、Groq Whisper STT 備援） | `discord_voice_engine.py`、`profile_compressor.py` | ⚠️ 不建議；是串流回應主力 |
| `openai` | **當成 OpenAI 相容 client 用**，打 Groq / Mistral / SambaNova / Together / OpenRouter / Gemini-compat（不是呼叫 OpenAI 本家） | `llm_pool.py`、`gemini_router.py`、`game_dict_manager.py` | ❌ LLM Bus 全靠它 |
| `duckduckgo_search` | 需要查網路時的搜尋 | `gemini_router_llm.py`（頂層 import，沒有 try/except） | ⚠️ 拿掉 router 模組會 import 失敗，要先改成可選 |

### 音樂

| 套件 | 用途 | 使用模組 | 可拿掉？ |
|---|---|---|---|
| `yt-dlp` | YouTube 搜尋與串流網址解析 | `cogs/music_cog.py`、`cogs/voice_controller.py`、`playlist_utils.py` | ❌ 音樂核心；要常更新（YouTube 常改） |
| `ytmusicapi` | YouTube Music 電台（無限續歌候選） | `ytmusic_radio.py` | ⚠️ 拿掉會失去自動續歌的一層 |
| `syncedlyrics` | 同步歌詞（DJ 串場、歌詞金句） | `cogs/music_cog_dj_lyrics.py`（有 try/except） | ✅ 可拿，失去歌詞類 DJ |
| `pypinyin` | 拼音比對（點歌同音字救援、intent 比對） | `music_fastpath.py`、`intent_agents/base.py`（有 try/except） | ⚠️ 可拿但點歌命中率會掉 |
| `RapidFuzz` | 模糊比對（喚醒詞變體、點歌、去重） | 8 個模組（多有 try/except） | ⚠️ 可拿但多處降級 |

### 記憶 / 儲存

| 套件 | 用途 | 使用模組 | 可拿掉？ |
|---|---|---|---|
| `chromadb` | 向量庫（跨 session 語意回憶），存在 `.chroma_db/` | `vector_store.py` | ❌ 目前頂層 import、無降級 |
| `aiofiles` | 非同步寫檔 | `suki_miner.py`（被 `gemini_router_content.py` 使用） | ✅ 小，改成 `asyncio.to_thread` 即可拿掉 |

### 網路 / 設定 / 其他

| 套件 | 用途 | 使用模組 | 可拿掉？ |
|---|---|---|---|
| `aiohttp` | HTTP client/server（Marmo、CompanionBridge、衛星 HTTP、封面、新聞、Spotify metadata） | 13 個 prod 模組 | ❌ |
| `websockets` | 雅婷台語雲端 STT 的 websocket | `yating_stt.py` | ✅ 雅婷已退役（`NAN_SPEAKER_IDS` 目前空值 → 這條路徑不會執行）。本身也是 google-genai 的間接依賴 |
| `python-dotenv` | 讀 `.env` | `main_discord.py`、`main_satellite.py`、`main_local*.py`、多數 scripts | ❌ |
| `PyYAML` | 讀 persona / 笑話庫 yaml | `persona_loader.py`、`joke_bank.py` | ❌ persona 需要 |
| `Pillow` | 音樂封面卡片、封面抽色 | `music_cover_card.py`、`cover_palette.py`、`cogs/music_cog_audio_meta.py` | ⚠️ 可拿但失去封面卡 |
| `psutil`（未安裝） | 記憶體壓力守門 | `memory_guard.py` | 見 §1a：目前沒裝＝功能失效 |

### 測試

| 套件 | 用途 | 可拿掉？ |
|---|---|---|
| `pytest` | 測試框架（`tests/`，404 個檔案） | prod 可不裝 |
| `pytest-asyncio` | async 測試（`@pytest.mark.asyncio`） | prod 可不裝 |
| `pytest-aiohttp` | `aiohttp_client` fixture（`test_marmo_server*.py`） | prod 可不裝 |

### 只有 scripts/ 用（requirements-scripts.txt）

| 套件 | 使用腳本 |
|---|---|
| `requests` | `build_music_catalog.py`、`musicbrainz_clean_music_memory.py` |
| `spotipy` | `spotify_clean_music_memory.py`、`spotify_connect_smoke_test.py` |
| `matplotlib` | `room_calibration.py` |
| `google-api-python-client`、`google-auth-oauthlib` | `run_gmail_calendar_sync.py`、`google_auth_setup.py` |
| `onnxruntime`（+ 未安裝的 `onnx`、`tensorflow`） | `onnx_to_tflite_wakeword.py` |
| `openwakeword`（未安裝） | `verify_wake_model.py`、`wake_over_music_poc.py` |

---

## 3. 系統層依賴（非 pip）

| 依賴 | 用途 | 目前狀態（這台 Mac） | 必要？ |
|---|---|---|---|
| **macOS + Apple Silicon** | Swift STT（Speech framework）只能在 macOS 跑 | macOS 27.0、arm64 | ✅ 主力 STT 綁 macOS |
| **Python** | — | prod：3.13.5（`venv_simon`）；CI 與 Dockerfile：**3.12**；`pyproject.toml`：`>=3.12` | ⚠️ prod 與 CI 版本不一致，CI 綠燈不代表 3.13 沒問題 |
| **ffmpeg / ffprobe** | 音樂與 TTS 解碼、響度正規化（26 處呼叫） | Homebrew ffmpeg 9.0.1 | ✅ |
| **libopus** | Discord voice Opus 編解碼 | Homebrew opus 1.6.1 | ✅ |
| **libsodium** | Discord voice 加密（PyNaCl） | Homebrew libsodium 1.0.21 / 1.0.22 | ✅ |
| **`macos_stt_v2_bin`** | 主力 STT：SpeechAnalyzer（macOS 26 新引擎）。`run_bot.py` 設 `STT_ENGINE_V2=true` | 已編譯、**有進 git**。建置：`swiftc -parse-as-library macos_stt_v2.swift -o macos_stt_v2_bin`（寫在原始檔開頭） | ✅ |
| **`macos_stt_bin`** | STT v1（SFSpeechRecognizer）；v2 空輸出時的備援，**喚醒偵測（wake check）固定走它** | 已編譯，但**被 .gitignore 排除、沒進 git**。建置指令：**待確認**（`macos_stt.swift` 內沒寫；`install-marvin.sh` 也沒編它） | ✅ 缺了喚醒偵測會壞 |
| `stream_stt_daemon_bin` / `stream_stt_shadow_bin` | 串流 STT 實驗（`STT_STREAMING`、`VOLATILE_SHADOW`，prod 皆設 false） | 已編譯、有進 git | ❌ 目前關閉 |
| **Xcode Command Line Tools**（`swiftc`） | 編譯上面的 Swift binary | 有 | 只有重編時需要 |
| **macOS `say`** | TTS 最後備援（edge-tts 被限流時） | 系統內建 | ✅ 備援 |
| **deno** | yt-dlp 解 YouTube 簽章（帶 cookies 時需要 JS runtime，見 `cogs/music_cog.py` 註解） | Homebrew | ✅ 音樂需要 |
| **YouTube cookies** | 繞過 YouTube 對這台 IP 的節流（`MARVIN_YT_COOKIES_FROM_BROWSER=chrome` 或 `MARVIN_YT_COOKIES_FILE`） | 設定中 | ⚠️ cookies 數週會過期 |
| **Ollama** | `LLM_PROVIDER=ollama` 時的本地 LLM（`OLLAMA_BASE_URL`，預設 `localhost:11434`） | **沒安裝**；串流路徑的程式碼註解寫「Ollama 已停用」；prod `LLM_PROVIDER=gemini` | ❌ 目前不需要 |
| **openclaw CLI（Node）** | NemoClaw（「龍蝦」）owner 專用 agent，`cogs/voice_controller.py` 以子程序呼叫 | nvm Node 22 下有安裝 | ❌ 只有 owner 功能用 |
| **launchd** | 常駐與自動重啟（見 OPERATOR.md） | 有 | ✅ 自架必要 |

---

## 4. ⚠️ `audioop` 在 Python 3.13 的風險

**事實**：
- Python 3.13 把 `audioop` 從標準函式庫**移除**（PEP 594）。
- 本 repo 自己的程式碼**沒有**直接 `import audioop`（已掃描確認）。
- 但依賴裡有兩處仍然 import 它：
  - `discord/player.py:30` 的 `import audioop`（`PCMVolumeTransformer` 用 `audioop.mul`）—— 這是 discord.py 語音播放模組，**import 就會觸發**，不是用到音量才觸發。
  - `discord/ext/voice_recv/sinks.py`、`extras/speechrecognition.py`。
- prod（3.13.5）能跑，是因為 discord.py 2.7.1 在 3.13 上會自動拉 **`audioop-lts`**（第三方社群維護的移植版）補回同名模組。
- `pytest.ini` 有一行 `filterwarnings` 在壓 `'audioop' is deprecated` 警告，那是 3.12 時代留下的。

**風險**：
1. **換環境沒裝到 `audioop-lts` 會直接起不來**：例如有人用 `--no-deps` 裝、自己 vendor discord.py、或降版到還沒宣告這個依賴的 discord.py，`import discord` 播放相關模組會 `ModuleNotFoundError`。這次已把 `audioop-lts==0.2.2; python_version >= "3.13"` 明列在 requirements.txt，讓這個依賴看得見。
2. **`audioop-lts` 是社群維護的套件，不是 CPython 官方**：它停更或出問題時，discord.py 語音會一起受影響。
3. **CI 在 3.12 跑**（`.github/workflows/ci.yml`、`Dockerfile` 都是 3.12），3.12 還有內建 `audioop`，所以 **CI 完全測不到這條路徑**。prod 是 3.13.5。
4. `discord-ext-voice_recv` 是 alpha 版，它對 `audioop` 的依賴什麼時候拿掉：待確認。

**建議**（還沒做，要你決定）：把 CI 與 Dockerfile 的 Python 對齊到 3.13，或至少在 CI 加一個 3.13 job。
