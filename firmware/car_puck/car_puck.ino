/*
 * car_puck.ino — Marvin ESP32-S3 車載 puck bring-up 骨架（Arduino）
 * 板：Goouuu ESP32-S3 N16R8 DevKit + S3 智能擴展板 V1.7
 *
 * 目的：板子到貨當天，「分步驗證」硬體 + 連線 + 麥克風 + 全鏈路，
 *      不用等 PCM5102/LiPo/喇叭。改下面 STEP 常數，一步一步點綠。
 *
 *   STEP 1 = PSRAM 檢查 + WiFi 連線          （驗你買對 N16R8 + 上網）
 *   STEP 2 = + HTTPS GET /now?t=token         （驗 Funnel 端到端 + S3 上 TLS）★最重要
 *   STEP 3 = + 三顆按鈕（GPIO0/38/39）
 *   STEP 4 = + INMP441 麥克風錄音到 PSRAM
 *   STEP 5 = + 按 PTT 錄 3s → POST /audio → 收 /reply（全鏈路，除了聽不到）
 *   STEP 6 = + PCM5102 I2S 喇叭輸出，放開 PTT 後自動輪詢 /reply 並就地播放（板子自己出聲）
 *   STEP 7 = + 開機自動上車：WiFi 連上→POST /car present（+30s 心跳）→常駐讀
 *            GET /audio_stream 播整個 mixer 輸出（音樂+TTS+DJ 全部，非單次 /reply
 *            輪詢；核心 0 專任務常駐讀、核心 1 跑 loop()/PTT，兩者不互卡）。
 *            斷電＝心跳自然停送，由伺服器 TTL(90s) 收尾停播，不用板子主動告知。
 *   STEP 8a = + 每 1s 輪詢 GET /car_commands?since=<seq>（見 main_satellite.py::
 *             handle_car_commands + marvin_voice_core/puck_command_queue.py），收到
 *             新指令只 log 到 Serial，完全不碰音訊路徑——edge端混音（雙 deck 自己
 *             crossfade，見 project 計畫文件）第一刀，只驗證「連得上新端點、seq 正確
 *             往前推進」，deck 邏輯留到下一刀再加。STEP<8 完全不受影響（沿用 STEP7
 *             既有的單一 /audio_stream 全混音路徑）。實機驗證過（2026-08-11）：四種
 *             指令 play/queue_next/crossfade/stop 皆收發正確。
 *   STEP 9  = + Deck B：獨立第二條 network+decode pipeline，收到 queue_next 指令才連
 *             GET /puck_deck?url=... 抓+解碼，但解碼出的 PCM 只做峰值統計 log，完全
 *             不寫 i2s_write——這一刀只驗證「同時撐住第二個 MP3 解碼器+第二條網路連線
 *             會不會影響/餓死既有 /audio_stream 播放」，真正疊加混音留到下一刀。收到
 *             stop 指令會關掉 deck B。STEP<9 完全不受影響，想退回只驗證過指令輪詢的
 *             狀態，改回 8 重新燒錄即可。
 *   STEP 10 = + 混音數學測試：deck B 解碼出的 PCM 額外寫進一個獨立的 PCM ring
 *             （deckBPcmRing），收到 crossfade 指令才真的算 gain（crossfadeGains()，
 *             照抄 device/puck_mixer.py::crossfade_gains() 邏輯）疊加進 deck A 輸出
 *             （mp3DataCallback 寫 i2s 前）。⚠️ deck A 的來源本身還是沒換——仍然吃
 *             /audio_stream（Mac 中央 mixer 已混好的完整輸出），這一刀純粹測試「疊加
 *             運算本身+兩個解碼器並行」的數學/CPU負擔對不對，不是真正的產品行為（正式
 *             行為要 deck A 也改吃 /puck_deck，那是之後單獨一刀，會動到主播放來源、
 *             風險更高）。crossfade 視窗跑完（elapsed>=duration）就把 crossfadeActive
 *             關掉，不做「切成 deck B 變主線」的部分。STEP<10 完全不受影響。
 *

 * ⚠️ 動手前要填：WiFi、MARVIN_TOKEN。（I2S 腳位已實測、不用再查，見下。）
 *
 * ── 2026-07-17 實機體檢結果（Goouuu N16R8 + V1.7，硬體全綠）──
 * efuse 實讀：ESP32-S3 QFN56 rev v0.2 / Flash 16MB / PSRAM 8MB (AP_3v3)，
 * 8MB PSRAM 開機後真的可用（psram free ≈ 8386096）＝STEP 1 的 PSRAM 檢查會過。
 * 下面三顆按鈕與 INMP441 三根腳都已按過/錄過音驗證（安靜 rms ~25、說話 rms 100-700）。
 * MAX98357 的三根腳仍只有 schematic 依據，還沒接喇叭實測。
 *
 * 🔥 燒錄流程（不照做會以為板子壞了）：
 *   1. 進下載模式＝按住 BOOT → 短按 RESET → 放 RESET → 放 BOOT
 *   2. arduino-cli upload -p /dev/cu.usbmodem1101 -b <FQBN 見下>
 *   3. ⚠️ 燒完手動按一下 RESET。esptool 印的「Hard resetting via RTS pin」
 *      在這塊板子不生效，不按的話晶片留在下載模式、app 不會跑、serial 全靜默，
 *      症狀跟「沒燒進去」或「板子壞了」一模一樣。
 *   FQBN: esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,CDCOnBoot=cdc,
 *         USBMode=hwcdc,PartitionScheme=app3M_fat9M_16MB
 *   ⚠️ USBMode 必須是 hwcdc：改成 default(TinyUSB) 在 macOS 上會 enumerate 但
 *      配不到驅動（ioreg 顯示 !matched）、根本不產生序列埠。
 *   ⚠️ HWCDC 會丟輸出：按鍵「短按」的列印常常整個消失。測按鍵要按住 ~3 秒；
 *      正式邏輯讀按鍵請用中斷/狀態機，別靠列印判斷。
 */

#include <WiFi.h>
#include <WiFiMulti.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <driver/i2s.h>
#include <freertos/semphr.h>
#include <string.h>
#include <MP3DecoderHelix.h>   // pschatzmann/arduino-libhelix — decode-only(不接管連線),
                                // 塞進既有 audioNetworkTask(填ring buffer)/audioPlaybackTask
                                // (解碼+i2s_write)雙task架構，見 audioPlaybackTask 前的說明

// ========== 你要填的 ==========
#define STEP 10  // ← 從 1 開始，每步綠了再 +1（10＝STEP 10 混音數學測試，見檔頭說明；
                 // 想退回只有deck B解碼統計、已驗證過的狀態，改回 9；想退回只有指令
                 // 輪詢，改回 8；想完全退回音訊路徑零改動的狀態，改回 7——每一層都
                 // 不用改別的地方，重新燒錄即可）

// 2026-07-25 懷疑：串流 debug 用的 Serial.printf 本身在 HWCDC 底下可能阻塞等 USB
// buffer（檔頭已知怪癖），會製造出我們正在追的那種週期性卡頓。先關掉排除，需要時開。
// 2026-08-11 重開查車上斷線：USB 接筆電、serial monitor 開著主動讀（不會積壓 buffer）
// 時應該安全；量完記得改回 0，避免平常沒接電腦時 printf 卡 HWCDC。
// 2026-08-11 追加：開了之後 ringUsed 持續飆高、puck 現場真的沒聲音——複現了上面那個
// 預言的症狀，改回 0 驗證是不是這個 flag 自己捅的婁子。
#define STREAM_DEBUG_PRINT 0

// 2026-07-26：家用WiFi + iPhone個人熱點都登記進去，WiFiMulti開機掃描自動選訊號最強、
// 有登記的那組——在家連家用WiFi（MARVIN_LOCAL_HOST區網明碼路徑成立），出門連iPhone
// 熱點（區網打不到、走[[Funnel回退]]，見 postAudio()/carHeartbeat()）。
WiFiMulti wifiMulti;
const char* WIFI_SSID    = "子嘉的Wi-Fi網路";
const char* WIFI_PASS    = "Jack1836";
const char* WIFI2_SSID   = "黃子嘉的iPhone Air";
const char* WIFI2_PASS   = "1cogjnsiqq1m";
const char* MARVIN_HOST  = "macbook-air.tail7ba8d0.ts.net";   // 不含 https://
const int   MARVIN_PORT  = 443;
const char* MARVIN_TOKEN = "FgmIGAdbKDJ9NCJUTY5qhRJe";        // ⚠️ 別 commit 真 token

// TEMP 實驗（2026-07-25）：/audio_stream 實測 sustained throughput 只有目標 187.5KB/s
// 的 ~55-67%（100-126KB/s），懷疑雙重加密——Tailscale WireGuard 本身已加密，這條又走
// HTTPS/TLS(443)，ESP32 軟體 mbedTLS 解密可能就是瓶頸。puck 目前只在家測（同一台
// Wi-Fi），先試直連 Mac 區網 IP + 明碼 HTTP，看 throughput 是否顯著改善來確認假設。
// 只有這條高頻寬串流走這個路徑，/car 心跳、/now 等低流量請求維持原本 HTTPS 不動。
// ⚠️ 只在家測試網路有效；真的出門用 4G 時這個 IP 打不通，需要退回 Tailscale/Funnel。
const char* MARVIN_LOCAL_HOST = "192.168.1.130";
const int   MARVIN_LOCAL_PORT = 8790;

// ========== 板上按鈕（V1.7；2026-07-17 三顆都實測按過）==========
#define PIN_BTN_PTT    0    // 喚醒/打斷 = 我們的 PTT
#define PIN_BTN_VOLUP  38
#define PIN_BTN_VOLDN  39

// ========== 板載 RGB 狀態燈（WS2812，核心板 GPIO48）==========
// ESP32 Arduino core 3.x 內建 neopixelWrite(pin,r,g,b)，不需函式庫。
// 用來顯示 Marvin 狀態：待機/收聽/播放/connected/錯誤。亮度刻意壓低（車上夜間不刺眼）。
#define PIN_RGB 48

// ========== INMP441 I2S 麥克風腳位（V1.7 schematic P6；2026-07-17 錄音實測通過）==========
#define I2S_MIC_SCK   5     // SCK / BCLK
#define I2S_MIC_WS    4     // WS / LRCLK
#define I2S_MIC_SD    6     // SD / DATA（麥→ESP32）；L/R 接地=左聲道

// ========== 喇叭輸出 I2S 腳位（V1.7 schematic P7；MAX98357/PCM5102 並到同一組）==========
// ⚠️ 這三根只有 schematic 依據，還沒實機播放驗證過（PCM5102 到貨但尚未接線通電測試）。
// PCM5102 的 SCK/FLT/DEMP/XSMT 是硬體接地/接高低電位決定模式，非 GPIO，軟體不用管。
#define I2S_AMP_BCLK  15
#define I2S_AMP_LRCLK 16
#define I2S_AMP_DIN   7
#define I2S_MIC_PORT  I2S_NUM_0
#define I2S_SPK_PORT  I2S_NUM_1   // 喇叭用獨立 I2S 埠，跟麥克風 I2S_NUM_0 不共用時脈

// ========== 錄音參數 ==========
#define SAMPLE_RATE   16000          // 16kHz mono 16-bit（STT 夠用、省 RAM）
#define MAX_REC_SECONDS 10                            // hold-to-talk 錄音上限（防 PSRAM buffer 溢出）
#define MAX_REC_SAMPLES (SAMPLE_RATE * MAX_REC_SECONDS)
#define MIN_REC_SAMPLES (SAMPLE_RATE / 4)             // < 0.25s 視為手滑，忽略不送

static int16_t* recBuf = nullptr;    // 放 PSRAM

// ========== 狀態燈狀態機 ==========
// 切狀態用 setLed()、每 loop 呼叫 updateLed() 畫動畫。
// 全程 millis()、零 delay，不打斷 hold-to-talk 錄音節奏。
enum LedState {
  LED_BOOT,       // 開機/連線中：黃色慢閃
  LED_CONNECTED,  // Marvin connected：綠色閃兩下 → 自動落回待機
  LED_STANDBY,    // 待機中：暗白呼吸
  LED_LISTENING,  // 收聽中（PTT 按住）：藍色常亮
  LED_PLAYING,    // 播放中（送出後等/播 /reply）：青色呼吸
  LED_ERROR,      // 錯誤（WiFi 斷/Funnel 非 200）：紅色快閃（持續，壞掉就該看起來壞）
};
static LedState ledState = LED_BOOT;
static uint32_t ledSince = 0;   // 進入當前狀態的時間

void setLed(LedState s) {
  if (s == ledState) return;
  ledState = s; ledSince = millis();
}

// 三角波呼吸：在 lo..hi 之間隨 period 週期起伏，回傳當前亮度
static uint8_t ledBreathe(uint32_t now, uint32_t period, uint8_t lo, uint8_t hi) {
  uint32_t ph = now % period, half = period / 2;
  uint32_t up = ph < half ? ph : period - ph;    // 0..half
  return lo + (uint32_t)(hi - lo) * up / half;
}

void updateLed() {
  uint32_t now = millis(), t = now - ledSince;
  switch (ledState) {
    case LED_BOOT: {                               // 黃色慢閃
      bool on = (now % 1000) < 500;
      neopixelWrite(PIN_RGB, on ? 40 : 0, on ? 28 : 0, 0); break;
    }
    case LED_CONNECTED: {                          // 綠色閃兩下 → 落回待機
      if (t >= 1200) { setLed(LED_STANDBY); break; }
      bool on = (t % 300) < 150;
      neopixelWrite(PIN_RGB, 0, on ? 60 : 0, 0); break;
    }
    case LED_STANDBY: {                            // 暗白呼吸（低亮度）
      uint8_t b = ledBreathe(now, 3000, 2, 16);
      neopixelWrite(PIN_RGB, b, b, b); break;
    }
    case LED_LISTENING:                            // 藍色常亮
      neopixelWrite(PIN_RGB, 0, 0, 80); break;
    case LED_PLAYING: {                            // 青色呼吸
      // STEP>=6：pollAndPlayReply() 播完/逾時會主動 setLed(LED_STANDBY)，這條 10s 只是兜底。
      // STEP<6（無喇叭）：沒有真播放事件，靠這個上限自動落回待機。
      if (t >= 10000) { setLed(LED_STANDBY); break; }
      uint8_t b = ledBreathe(now, 1500, 4, 70);
      neopixelWrite(PIN_RGB, 0, b, b); break;
    }
    case LED_ERROR: {                              // 紅色快閃
      bool on = (now % 300) < 150;
      neopixelWrite(PIN_RGB, on ? 90 : 0, 0, 0); break;
    }
  }
}

// ------------------------------------------------------------------
void connectWiFi() {
  Serial.println("[WiFi] 連線（自動選家用WiFi/iPhone熱點訊號較強的那組）...");
  WiFi.mode(WIFI_STA);
  wifiMulti.addAP(WIFI_SSID, WIFI_PASS);
  wifiMulti.addAP(WIFI2_SSID, WIFI2_PASS);
  uint32_t t0 = millis();
  while (wifiMulti.run() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300); Serial.print("."); updateLed();   // 連線期間 setup 阻塞，靠這裡讓黃燈慢閃
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] OK, SSID=%s IP=%s RSSI=%d\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());
    // ⚠️ 2026-07-24 實測過 WiFi.setSleep(false)：串流反而更破碎（只剩零星爆音）、
    // 心跳一度斷拍近90秒（疑似跟其他 WiFi 功能衝突，社群也有類似回報），已復原移除。
    // 2026-07-25 重試：架構整個換了（network/playback 分兩個 task + 1MB ring buffer +
    // 明碼直連區網 IP），舊測試的前提已經不成立，modem sleep 週期性喚醒延遲很符合我們
    // 現在量到的 170-400ms maxGap 規律性卡頓特徵，值得在新架構上重新單獨驗證一次。
    WiFi.setSleep(false);
  } else {
    Serial.println("[WiFi] ❌ 連不上，檢查 SSID/密碼/熱點開了沒");
    setLed(LED_ERROR);
  }
}

// STEP 2：HTTPS GET /now?t=token —— 驗 Funnel 端到端 + TLS
void testFunnelNow() {
  WiFiClientSecure client;
  client.setInsecure();   // bring-up 先跳過憑證驗證（Funnel 是有效 LetsEncrypt，之後可加 CA）
  // ⚠️ WiFiClientSecure 預設 handshake_timeout=120000ms，跟 connect() 的 timeout 參數是
  // 兩回事（那個只管 TCP 連線階段）——握手卡住最長會空等 2 分鐘，這裡先設短。
  client.setHandshakeTimeout(5);
  Serial.println("[HTTPS] 連 Funnel ...");
  if (!client.connect(MARVIN_HOST, MARVIN_PORT)) {
    char errBuf[128];
    client.lastError(errBuf, sizeof(errBuf));
    Serial.printf("[HTTPS] ❌ TLS 連不上（TLS 太重/沒網/Funnel 沒開）lastError=%s\n", errBuf);
    setLed(LED_ERROR);
    return;
  }
  String req = String("GET /now?t=") + MARVIN_TOKEN + " HTTP/1.1\r\n" +
               "Host: " + MARVIN_HOST + "\r\nConnection: close\r\n\r\n";
  client.print(req);
  String statusLine = client.readStringUntil('\n');
  Serial.printf("[HTTPS] 回應：%s", statusLine.c_str());
  if (statusLine.indexOf("200") > 0) {
    Serial.println("[HTTPS] ✅ 端到端通了！token 對、Funnel 對、TLS 沒問題");
    setLed(LED_CONNECTED);   // 綠色閃兩下 → 自動落回待機
  } else if (statusLine.indexOf("401") > 0) {
    Serial.println("[HTTPS] ⚠️ 401 = 通了但 token 錯，改 MARVIN_TOKEN");
    setLed(LED_ERROR);
  } else {
    setLed(LED_ERROR);
  }
  client.stop();
}

// STEP 4：起 INMP441 I2S 麥
void startMic() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,   // INMP441 送 24-bit 在 32-bit 框
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
    .use_apll = true,          // 專用音訊 PLL，降 jitter（審查建議）
  };
  i2s_pin_config_t pins = {
    // ⚠️ mck_io_num 必須明確設 NO_CHANGE：i2s_pin_config_t 第一個欄位就是它，
    // 漏設會被零初始化成 0=GPIO0，I2S 把 MCLK 輸出到 GPIO0＝徵用掉 PTT 腳，
    // 一開麥克風 GPIO0 就被拉死在 LOW→PTT 無限誤觸發（2026-07-17 診斷實錘：
    // 啟 I2S 前 GPIO0 low 0%、啟後 low 100%；設此行後回 0%）。INMP441 不需 MCLK。
    .mck_io_num = I2S_PIN_NO_CHANGE,
    .bck_io_num = I2S_MIC_SCK, .ws_io_num = I2S_MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE, .data_in_num = I2S_MIC_SD,
  };
  i2s_driver_install(I2S_MIC_PORT, &cfg, 0, NULL);
  i2s_set_pin(I2S_MIC_PORT, &pins);
  Serial.println("[MIC] INMP441 I2S 起動");
}

// STEP 6：起 PCM5102 I2S 喇叭輸出（TX）
void startSpeaker() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = 48000,                          // 對齊 BrowserSpeakerOutput 輸出格式；實際播放前會用 i2s_set_clk 覆蓋成 wav 標頭裡的值
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    // 2026-07-25：曾試過改 mono 省頻寬，後來確認真瓶頸是雙重 TLS 解密+mixer 端即時解碼
    // underrun，跟聲道數無關；改回 stereo（伺服器端 StreamSpeakerOutput 也已改回預設）。
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,  // stereo
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 12,   // 2026-07-25 從 8 拉到 12：給網路串流抖動多一點 DMA 端容錯
    .dma_buf_len = 512,
    .use_apll = true,
  };
  i2s_pin_config_t pins = {
    .mck_io_num = I2S_PIN_NO_CHANGE,   // PCM5102 SCK 腳硬體接地走內部 PLL，不需 MCLK
    .bck_io_num = I2S_AMP_BCLK, .ws_io_num = I2S_AMP_LRCLK,
    .data_out_num = I2S_AMP_DIN, .data_in_num = I2S_PIN_NO_CHANGE,
  };
  i2s_driver_install(I2S_SPK_PORT, &cfg, 0, NULL);
  i2s_set_pin(I2S_SPK_PORT, &pins);
  Serial.println("[SPK] PCM5102 I2S 起動");
}

// 解 RIFF/WAVE 標頭：找 fmt/data chunk，不假設固定 44-byte 標頭長度（穩健對付上游格式微調）
static bool parseWav(uint8_t* buf, size_t len, size_t* dataOff, size_t* dataLen,
                      uint32_t* sr, uint16_t* ch, uint16_t* bits) {
  if (len < 12 || memcmp(buf, "RIFF", 4) != 0 || memcmp(buf + 8, "WAVE", 4) != 0) return false;
  size_t p = 12;
  bool haveFmt = false;
  while (p + 8 <= len) {
    char id[4]; memcpy(id, buf + p, 4);
    uint32_t csz; memcpy(&csz, buf + p + 4, 4);
    size_t body = p + 8;
    if (memcmp(id, "fmt ", 4) == 0 && body + 16 <= len) {
      memcpy(ch, buf + body + 2, 2);
      memcpy(sr, buf + body + 4, 4);
      memcpy(bits, buf + body + 14, 2);
      haveFmt = true;
    } else if (memcmp(id, "data", 4) == 0) {
      *dataOff = body; *dataLen = min((size_t)csz, len - body);
      return haveFmt;
    }
    if (body + csz > len) break;
    p = body + csz + (csz & 1);   // chunk 對齊 word boundary
  }
  return false;
}

// STEP 6：POST /audio 成功後呼叫。輪詢 GET /reply?since=<seq>，收到就播、逾時就放棄。
// 阻塞式（沿用 STEP 5 live 版驗證過的作法），輪詢/播放期間持續 updateLed() 讓青燈呼吸不凍。
static uint32_t lastReplySeq = 0;

void pollAndPlayReply() {
  const uint32_t timeoutMs = 20000, pollMs = 300;
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    HTTPClient http;
    WiFiClientSecure client; client.setInsecure();
    client.setHandshakeTimeout(5);   // 見 testFunnelNow() 前的註解：預設120s跟connect()逾時無關
    const char* headerKeys[] = { "X-Reply-Seq" };
    http.collectHeaders(headerKeys, 1);
    String url = String("https://") + MARVIN_HOST + "/reply?since=" + lastReplySeq + "&t=" + MARVIN_TOKEN;
    http.begin(client, url);
    int code = http.GET();

    if (code == 200) {
      int len = http.getSize();
      uint8_t* wav = (len > 0) ? (uint8_t*)ps_malloc(len) : nullptr;
      if (wav) {
        WiFiClient* stream = http.getStreamPtr();
        int got = 0;
        while (got < len && http.connected()) {
          got += stream->readBytes(wav + got, len - got);
        }
        String seqHdr = http.header("X-Reply-Seq");
        http.end();
        if (seqHdr.length()) lastReplySeq = (uint32_t)seqHdr.toInt();
        size_t dataOff, dataLen; uint32_t sr; uint16_t ch, bits;
        if (parseWav(wav, got, &dataOff, &dataLen, &sr, &ch, &bits)) {
          Serial.printf("[SPK] 播放 %u Hz / %uch / %u-bit / %u bytes\n", sr, ch, bits, (unsigned)dataLen);
          i2s_set_clk(I2S_SPK_PORT, sr, (i2s_bits_per_sample_t)bits,
                      ch == 1 ? I2S_CHANNEL_MONO : I2S_CHANNEL_STEREO);
          size_t written, off = 0;
          while (off < dataLen) {
            i2s_write(I2S_SPK_PORT, wav + dataOff + off, dataLen - off, &written, portMAX_DELAY);
            off += written;
            updateLed();   // 播放期間讓青燈呼吸繼續動
          }
        } else {
          Serial.println("[SPK] ⚠️ wav 標頭解析失敗，跳過播放");
        }
        free(wav);
      } else {
        if (len > 0) Serial.println("[SPK] ⚠️ ps_malloc 失敗，跳過播放");
        String seqHdr = http.header("X-Reply-Seq");
        http.end();
        if (seqHdr.length()) lastReplySeq = (uint32_t)seqHdr.toInt();
      }
      setLed(LED_STANDBY);
      return;
    }
    http.end();
    delay(pollMs);
    updateLed();
  }
  Serial.println("[SPK] /reply 逾時（20s 沒等到回覆）");
  setLed(LED_STANDBY);
}

// STEP 7：常駐音訊串流（車載模式）——開機後常駐一條到 /audio_stream 的連線，讀到就播；
// 跑在獨立 FreeRTOS 任務、釘死 core 0（Arduino loop()/PTT/心跳跑在預設的 core 1），
// 讀取阻塞不會卡住 PTT 錄音或心跳送出。/audio_stream 是整個 mixer 輸出（音樂+TTS+DJ
// 全部），車載模式下伺服器 /reply 已停用，對話回覆也會自動從這條串流播出，postAudio()
// 不用再額外輪詢 /reply（見下）。
// 手動解 chunked transfer-encoding（伺服器固定用 aiohttp StreamResponse，會送 chunked；
// 不支援時直接放棄這輪連線重試，不猜格式）。
//
// 2026-07-25：拆成兩個任務、中間隔一個 PSRAM ring buffer，網路讀取（audioNetworkTask）
// 跟 I2S 播放（audioPlaybackTask）不再共用同一個迴圈——舊版兩者綁在一起時，WiFi 封包
// 抖動/HTTP 卡頓會直接變成 I2S 斷供、聲音破碎（DMA 只吐得出「剛好收到的那一點」）。現在
// WiFi 卡頓時 playback 繼續吃 ring buffer 存量，不影響出聲；network task 補滿 buffer
// 不受播放節奏影響。不需要即時性（純音樂播放器用途），拉大 buffer 換穩定完全划算。
// N16R8 有 8MB PSRAM，1MiB 只吃 12.5%（跟 10s 錄音 buffer ~312KB 加起來還不到一半），
// 拉滿一點換更長的抖動緩衝時間（stereo 48k/16bit ~5.5s）划算。開播門檻跟總容量脫鉤、
// 固定抓 ~0.5s：只是要吸收 WiFi 瞬間卡頓，不需要每次斷流重蓄都等半天。
// 2026-07-25 追加：曾試拉到 4MiB 想換更長抗抖動空間，但同一輪實測重連變得更頻繁+
// 中斷變更長，先退回 1MiB 做對照排除「buffer 大小本身導致不穩」這個變因，只留
// audioNetworkTask 的「重連不強制重蓄」修法。等對照測試結果出來再決定要不要重新拉大。
// 2026-07-26：/audio_stream 改送 MP3（128kbps，見 server 端 Mp3StreamEncoder），ring
// buffer 現在裝的是壓縮 bytes，不是原始 PCM——同樣 1MiB 現在能撐 ~64s 音訊（原本
// stereo PCM 只能撐 ~5.5s），STREAM_PREBUF_BYTES 改用 bitrate 換算維持 ~0.5s 開播延遲
// （不再是「越大越抗抖動」，MP3 bitrate 遠低於 187.5KB/s 需求，抗抖動空間本來就大增）。
#define MP3_BITRATE_KBPS 128
#define STREAM_RING_SIZE (1024 * 1024)
#define STREAM_PREBUF_BYTES ((MP3_BITRATE_KBPS * 1000 / 8) / 2)   // ~0.5s
static uint8_t* streamRing = nullptr;
// 2026-07-25 診斷用：查 pbuf_free 崩潰根因，抓三顆任務的堆疊水位一起比對
// （見 carHeartbeat() 尾巴的 [StackWM] log）。
static TaskHandle_t audioNetTaskHandle = nullptr;
static TaskHandle_t audioPlayTaskHandle = nullptr;
static TaskHandle_t loopTaskHandle = nullptr;

// 2026-07-25 方案 A（隔離測試證實：拔掉心跳、只留 audioNetworkTask 31 分鐘零崩潰，
// 心跳的 connect/POST/close 週期是 pbuf_free 崩潰的必要條件）：
// 鎖只保護「真正的 byte 搬移」那一瞬間（client.read() 在 available()>0 時只是把
// lwIP 已經收下的資料 memcpy 出來，微秒級），絕對不鎖任何等待資料/連線的迴圈——
// 前一版鎖住 readStringUntil()/http.POST() 這種可能空等到 client 逾時（20s）的呼叫，
// 等於持鎖阻塞 20 秒，才會撞 task watchdog。這版所有讀取都先用 available() 非阻塞
// 判斷，沒資料就不鎖、vTaskDelay(1) 讓出 CPU，只有確定有資料在等時才進鎖。
static SemaphoreHandle_t lwipMutex = nullptr;
#define LWIP_LOCK()   xSemaphoreTake(lwipMutex, portMAX_DELAY)
#define LWIP_UNLOCK() xSemaphoreGive(lwipMutex)

// 非阻塞讀一行（到 \n，行尾 \r 會被去掉）：沒資料就不鎖、讓出 CPU 再檢查；
// 有資料才進鎖搬。timeoutMs=0 代表不設逾時上限（仍會持續 yield，不忙迴圈）。
// 回傳 false＝連線斷了或逾時前沒等到換行。
// ⚠️ 2026-07-26：這裡原本的假設是「client.available() 不碰網路層，不用鎖」——對明碼
// WiFiClient 成立（純查 OS socket buffer），但 WiFiClientSecure.available() 底層會呼叫
// mbedtls_ssl_read(...,0) 真的碰 recv()／lwIP 狀態。沒鎖住它，就跟 carHeartbeatTask
// 在另一個 core 的 lwIP 操作沒有互斥保護——實機重現症狀是音訊「電流爆點聲、聽不出
// 內容」（不是逾時／不是卡死，調 timeout 值沒用），研判是資料撕裂。連 available() 一起
// 鎖住（對明碼 client 而言鎖這個幾乎零成本，一起鎖沒有壞處）。
static bool lockedReadLine(WiFiClient& client, String& out, uint32_t timeoutMs) {
  out = "";
  uint32_t t0 = millis();
  for (;;) {
    LWIP_LOCK();
    bool hasData = client.available() > 0;
    int c = hasData ? client.read() : -1;
    LWIP_UNLOCK();
    if (hasData) {
      if (c < 0) { vTaskDelay(pdMS_TO_TICKS(1)); continue; }
      if (c == '\n') return true;
      if (c != '\r') out += (char)c;
      continue;
    }
    if (!client.connected()) return false;
    if (timeoutMs > 0 && millis() - t0 > timeoutMs) return false;
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

// 非阻塞讀最多 want bytes：沒資料就不鎖、讓出 CPU；有資料才進鎖搬（一次搬
// available() 跟剩餘需求的較小值）。回傳實際讀到的 byte 數（可能 < want，逾時/斷線）。
static size_t lockedReadBytes(WiFiClient& client, uint8_t* buf, size_t want, uint32_t timeoutMs) {
  size_t got = 0;
  uint32_t t0 = millis();
  while (got < want) {
    // available() 一起鎖住，理由見 lockedReadLine() 前的 2026-07-26 註解。
    LWIP_LOCK();
    int avail = client.available();
    int n = 0;
    if (avail > 0) {
      size_t chunk = (size_t)avail < (want - got) ? (size_t)avail : (want - got);
      n = client.read(buf + got, chunk);
    }
    LWIP_UNLOCK();
    if (n > 0) { got += (size_t)n; t0 = millis(); continue; }
    if (!client.connected()) break;
    if (timeoutMs > 0 && millis() - t0 > timeoutMs) break;
    vTaskDelay(pdMS_TO_TICKS(1));
  }
  return got;
}
static volatile size_t ringHead = 0, ringTail = 0;   // 單一 producer（network）寫 head、單一 consumer（playback）讀 tail
static volatile bool streamPrimed = false;           // 是否已蓄滿過一輪，允許開始播放
// 2026-07-26：MP3 化之後每次重連都要處理——每個 HTTP 連線在 server 端都是全新一份
// Mp3StreamEncoder（見 handle_audio_stream），舊連線最後幾個 bytes 可能卡在 MP3 frame
// 中間，直接接上新連線開頭的 bytes 會讓 decoder 的 frame_buffer 對不齊，findSynchWord
// 誤認出假的 sync word，症狀＝MP3 格式忽快忽慢亂跳（實機驗證過：raw PCM 時代這裡靠
// 「ring 還有存量就不強制重蓄」換無感重連是對的，MP3 沒有這個彈性，byte stream 邊界
// 一定要跟 decoder 重置對齊）。改成每次重連無條件清空 ring + 標記需要重置 decoder。
static volatile bool mp3NeedsReset = false;

static inline size_t ringUsed() {
  size_t h = ringHead, t = ringTail;
  return (h >= t) ? (h - t) : (STREAM_RING_SIZE - t + h);
}
static inline size_t ringFree() { return STREAM_RING_SIZE - 1 - ringUsed(); }
static inline void ringReset() { ringHead = 0; ringTail = 0; }

// memcpy 版（最多跨 wraparound 分兩段拷貝），取代逐 byte 迴圈——逐 byte 版每個位元組都要
// 算一次 modulo，512 位元組一次呼叫的額外開銷跟實際音訊時長（~2.7ms）相比不算小，加上
// 128KB 版本的小 buffer，兩者疊加就是使用者聽到的週期性 ~0.3s 卡頓（2026-07-25 實測）。
static void ringWrite(const uint8_t* data, size_t len) {
  size_t offset = 0;
  while (offset < len) {
    while (ringFree() == 0) vTaskDelay(pdMS_TO_TICKS(1));   // playback 消化不及才等（backpressure）
    size_t chunk = len - offset;
    size_t freeNow = ringFree();
    if (chunk > freeNow) chunk = freeNow;
    size_t firstPart = STREAM_RING_SIZE - ringHead;
    if (firstPart > chunk) firstPart = chunk;
    memcpy(streamRing + ringHead, data + offset, firstPart);
    if (chunk > firstPart) memcpy(streamRing, data + offset + firstPart, chunk - firstPart);
    ringHead = (ringHead + chunk) % STREAM_RING_SIZE;
    offset += chunk;
  }
}

static size_t ringRead(uint8_t* out, size_t maxLen) {
  size_t avail = ringUsed();
  size_t want = maxLen < avail ? maxLen : avail;
  if (want == 0) return 0;
  size_t firstPart = STREAM_RING_SIZE - ringTail;
  if (firstPart > want) firstPart = want;
  memcpy(out, streamRing + ringTail, firstPart);
  if (want > firstPart) memcpy(out + firstPart, streamRing, want - firstPart);
  ringTail = (ringTail + want) % STREAM_RING_SIZE;
  return want;
}

void audioNetworkTask(void* pv) {
  Serial.println("[Stream] audioNetworkTask 已啟動");
  // 2026-07-25：512→4096。實測 idle 靜音期間 ring buffer 仍穩定下探（96KB→3.8KB/~2s，
  // 實際補貨速率只有需求的 ~75%），優先權已經調過還是一樣，指向每次 readBytes 的 TLS
  // 解密呼叫開銷才是瓶頸，不是排程。加大單次讀取量減少呼叫次數換 throughput。
  uint8_t buf[4096];
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) { vTaskDelay(pdMS_TO_TICKS(500)); continue; }

    Serial.printf("[Stream] connect() 前 free heap=%u minFree=%u\n",
                  (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap());
    // 2026-07-26：先試區網明碼直連（快、跳過TLS解密開銷，家用WiFi成立），短逾時(1.2s)
    // 快速失敗；連不到（出門用4G/熱點）就退回 Funnel TLS。不記狀態每輪都重試區網——
    // reconnect本來就頻繁（1-5分鐘一次），這樣回到家用網路範圍會自動切回區網，不用
    // 額外邏輯判斷「現在該用哪條」。WiFiClientSecure繼承WiFiClient，用base reference
    // 讓下面 lockedReadLine/lockedReadBytes 這段共用邏輯不用為兩條路徑各寫一份。
    WiFiClient localClient;
    WiFiClientSecure funnelClient; funnelClient.setInsecure();
    // ⚠️ 2026-07-26 實機重現：handshake_timeout 預設120s跟connect()的timeout參數是兩回事
    // （後者只管TCP連線階段），握手卡住的話 LWIP_LOCK() 會被鎖住到120秒——carHeartbeat/
    // PTT/ring buffer補貨全部餓死在等同一把鎖，症狀＝出門連上熱點後播不到一秒就整個
    // 靜音、心跳log也停。設短逾時，見 testFunnelNow() 前的註解。
    funnelClient.setHandshakeTimeout(5);
    LWIP_LOCK();
    bool connectOk = localClient.connect(MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 1200);
    LWIP_UNLOCK();
    bool useFunnel = !connectOk;
    if (useFunnel) {
      LWIP_LOCK();
      connectOk = funnelClient.connect(MARVIN_HOST, MARVIN_PORT, 5000);
      LWIP_UNLOCK();
    }
    WiFiClient& client = useFunnel ? (WiFiClient&)funnelClient : localClient;
    if (!connectOk) {
      Serial.println("[Stream] ⚠️ connect()（區網+Funnel都失敗），2s 後重試");
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }
    const char* host = useFunnel ? MARVIN_HOST : MARVIN_LOCAL_HOST;
    if (useFunnel) Serial.println("[Stream] 區網打不到，走Funnel連線");
    // ⚠️ 2026-07-26 實機重現：lockedReadLine/lockedReadBytes 的「available()>0 才進鎖」
    // 設計是對明碼 socket 驗證過的——明碼 available() 純查 OS buffer 真的零阻塞。但
    // WiFiClientSecure.available() 底層是 mbedtls_ssl_read(...,0) 嘗試解密一個 TLS
    // record，會真的等 socket recv()，逾時長度＝這個 client 的 SO_RCVTIMEO（沿用
    // connect() 設的 5000ms）——等於「非阻塞輪詢」一次就可能卡 5 秒，症狀＝Funnel
    // 連線可以連上，但读回的極慢/像卡死，音樂不到一秒就斷。
    // ⚠️ 2026-07-26 第二輪：一開始只調這裡的 timeout（100ms→2000ms 都試過），board不再
    // 卡死但音訊仍是「電流爆點聲、聽不出內容」——後來查到 lockedReadLine/lockedReadBytes
    // 的 available() 沒上鎖（見上方定義前的註解）才是真正原因，鎖住後這個 timeout 只是
    // 控制輪詢節奏，不影響正確性，收在 500ms（明碼 client 設這個沒副作用，一起設）。
    // ⚠️ 2026-07-26 第三輪：加了鎖之後爆音依舊沒消失——實測量到 Funnel+熱點實際
    // throughput 只有 ~60KB/s（目標187.5KB/s stereo），ring buffer 持續被榨到接近見底
    // （maxGap 300-470ms，健康時10.6ms），爆音其實是buffer underrun反覆重新填充的
    // 不連續交界，不是資料撕裂。真正的修法要降低串流本身的bitrate（mono downmix/降
    // 取樣率），需要同時改server端跟firmware動態讀格式header設I2S，範圍更大、留待下次。
    client.setTimeout(500);
    // mixer 是 on-demand 來源，兩幀之間偶爾會停超過 1s。舊版靠 client.setTimeout(20000)
    // 讓 Arduino 內建的 readStringUntil/readBytes 空等到 20s——2026-07-25 改用
    // lockedReadLine/lockedReadBytes（見上方定義），逾時改由呼叫端明講的 timeoutMs
    // 參數控制，setTimeout 已無作用、拿掉。
    String req = String("GET /audio_stream?t=") + MARVIN_TOKEN + " HTTP/1.1\r\n" +
                 "Host: " + host + "\r\nConnection: close\r\n\r\n";
    LWIP_LOCK(); client.print(req); LWIP_UNLOCK();

    String status;
    lockedReadLine(client, status, 20000);
    bool ok200 = status.indexOf("200") > 0;
    bool chunked = false;
    while (client.connected()) {
      String h;
      if (!lockedReadLine(client, h, 20000)) break;   // 逾時/斷線＝跳出，走下面的重連
      if (h.length() == 0) break;                     // 空行＝標頭結束
      if (h.indexOf("chunked") >= 0) chunked = true;
    }

    if (!ok200 || !chunked) {
      Serial.printf("[Stream] /audio_stream 非預期回應（200=%d chunked=%d）：%s",
                    ok200, chunked, status.c_str());
      LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
      vTaskDelay(pdMS_TO_TICKS(5000));        // 車載模式未接串流輸出等更久，避免熱迴圈
      continue;
    }
    Serial.println("[Stream] /audio_stream 連上，開始灌 buffer");
    // 2026-07-25 raw PCM 時代的邏輯：ring 還有存量就不強制重蓄，讓 audioPlaybackTask
    // 繼續吃舊資料撐過重連、聽不出斷點。2026-07-26 MP3 化後這個優化不能再用——每個
    // HTTP 連線在 server 端都是全新一份 Mp3StreamEncoder，舊連線尾巴的 bytes 接上新
    // 連線開頭的 bytes 對 decoder 來說是同一個 byte stream 裡的不連續斷點，frame_buffer
    // 對不齊會讓 findSynchWord 誤認假 sync word（實機驗證過：MP3 格式忽快忽慢亂跳）。
    // 改成每次重連無條件標記需要重置，犧牲一點點無感重連（MP3 bitrate 低，
    // STREAM_PREBUF_BYTES 現在只有 ~0.5s，可犧牲的存量本來就不多）換正確性。
    // ⚠️ ring 是 single-producer(network)/single-consumer(playback) 設計，ringTail
    // 只能由 playback task 動——這裡不直接呼叫 ringReset()（會兩邊都改 head/tail，
    // 破壞這個約定，playback 端讀到「head 已歸零、tail 還沒歸零」的中間態會算出離譜
    // 大的 ringUsed()，等於製造更嚴重的資料錯位）。只設旗標，實際丟棄舊資料交給
    // audioPlaybackTask 自己用 ringTail = ringHead 追上。
    streamPrimed = false;
    mp3NeedsReset = true;

    while (client.connected()) {
      String sizeLine;
      if (!lockedReadLine(client, sizeLine, 20000)) sizeLine = "";  // 逾時/斷線走下面統一當異常處理
      long chunkSize = strtol(sizeLine.c_str(), nullptr, 16);
      if (chunkSize <= 0) {
        // 2026-07-25：重連頻率實測每 1-5 分鐘一次、貫穿整晚，跟播放內容/歌曲轉場無關
        // （見 project_car_puck_pops_full_fix_2026-07-25）。斷線當下不知道是①這裡 readBytes
        // 逾時讀到殘缺 size-line（client 端自己判斷異常放棄）還是②WiFi/socket 真斷——
        // 分不清就無法對症下藥，這裡補印 raw size-line + connected 狀態 + RSSI + ring 存量。
        Serial.printf("[Stream] chunk-size 異常 break：raw='%s' len=%d connected=%d rssi=%d ringUsed=%u\n",
                      sizeLine.c_str(), sizeLine.length(), (int)client.connected(),
                      WiFi.RSSI(), (unsigned)ringUsed());
        break;              // 終止 chunk（0）或連線壞了，跳出重連
      }
      long remain = chunkSize;
      while (remain > 0 && client.connected()) {
        size_t want = remain < (long)sizeof(buf) ? (size_t)remain : sizeof(buf);
        // 2026-07-25：lockedReadBytes 內部已經是「沒資料就 vTaskDelay(1) 讓出 CPU、
        // 不忙迴圈」，n<=0 代表這裡設的 20s 逾時內完全沒有任何進展（真的斷了/伺服器
        // 卡很久）——舊版同樣邏輯的顧慮（餓死 IDLE0 觸發 task watchdog）在這裡不成立，
        // 因為讓出 CPU 的動作已經內建在 lockedReadBytes 的等待迴圈裡。
        size_t n = lockedReadBytes(client, buf, want, 20000);
        if (n == 0) {            // 逾時空讀、連線仍在＝重試；break 會丟下沒讀完的 remain
                                 // 位元組，後面 readStringUntil 撈到 PCM 當文字解析，
                                 // 永久錯位整條連線的 chunk 邊界（2026-07-24 實測：
                                 // 放出來的音訊像迴圈亂碼、聽不出完整單字）
          vTaskDelay(pdMS_TO_TICKS(1));
          continue;
        }
        ringWrite(buf, n);       // 只塞 buffer，不碰 I2S——播放節奏交給 audioPlaybackTask
        remain -= (long)n;

#if STREAM_DEBUG_PRINT
        // 2026-07-25 懷疑：Serial.printf 本身在 HWCDC 底下可能會阻塞等 USB buffer
        // （這塊板子的已知怪癖，見檔頭註解），變成我們在追的週期性卡頓的兇手之一。
        // 先關掉驗證，需要時再開。
        static size_t _bytesSinceLog = 0;
        static uint32_t _lastLogMs = 0;
        _bytesSinceLog += n;
        uint32_t now = millis();
        if (now - _lastLogMs >= 1000) {
          Serial.printf("[Stream] recv %.1f KB/s（目標 187.5 KB/s）ringUsed=%u\n",
                        _bytesSinceLog / 1024.0 * 1000.0 / (now - _lastLogMs), (unsigned)ringUsed());
          _bytesSinceLog = 0;
          _lastLogMs = now;
        }
#endif
      }
      { String _crlf; lockedReadLine(client, _crlf, 20000); }   // chunk 尾 CRLF
    }
    if (!client.connected()) {
      Serial.printf("[Stream] 外層迴圈退出：client.connected()==false rssi=%d ringUsed=%u\n",
                    WiFi.RSSI(), (unsigned)ringUsed());
    }
    LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
    Serial.println("[Stream] /audio_stream 斷線，1s 後重連");
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

#if STEP >= 10
// ============================================================
// STEP 10：混音狀態 + deck B 解碼 PCM ring——放在 mp3DataCallback（deck A）之前，
// 因為它需要讀這些東西；deck B 那組（本節之後、STEP>=9 區塊裡的 mp3DataCallbackB）
// 只是往這裡寫，兩邊共用同一份宣告，避免 Arduino 對「變數」（不像函式）不會自動產生
// forward declaration 而編譯失敗。
// ============================================================
#define DECKB_PCM_RING_SIZE (48000 * 2 * 2 * 6)   // 6秒 stereo s16 @48kHz ≈ 1.1MB
static uint8_t* deckBPcmRing = nullptr;
static volatile size_t deckBPcmHead = 0, deckBPcmTail = 0;

static volatile bool crossfadeActive = false;
static volatile uint32_t crossfadeStartMs = 0;
static volatile uint32_t crossfadeDurationMs = 4000;

static inline size_t deckBPcmUsed() {
  size_t h = deckBPcmHead, t = deckBPcmTail;
  return (h >= t) ? (h - t) : (DECKB_PCM_RING_SIZE - t + h);
}
static inline size_t deckBPcmFree() { return DECKB_PCM_RING_SIZE - 1 - deckBPcmUsed(); }

// 跟其他 ring 不同：滿了就丟這批，不 vTaskDelay 等（PCM ring 只在 crossfade 附近才
// 有人消費，deck B 解碼 task 不該被這裡卡住——寧可丟音訊樣本，不要拖累 deck B 自己
// 的 decode 節奏）。
static void deckBPcmWrite(const uint8_t* data, size_t len) {
  if (deckBPcmRing == nullptr) return;   // ps_malloc 失敗時安靜跳過，別寫進空指標
  size_t freeNow = deckBPcmFree();
  if (freeNow == 0) return;
  size_t chunk = len > freeNow ? freeNow : len;
  size_t firstPart = DECKB_PCM_RING_SIZE - deckBPcmHead;
  if (firstPart > chunk) firstPart = chunk;
  memcpy(deckBPcmRing + deckBPcmHead, data, firstPart);
  if (chunk > firstPart) memcpy(deckBPcmRing, data + firstPart, chunk - firstPart);
  deckBPcmHead = (deckBPcmHead + chunk) % DECKB_PCM_RING_SIZE;
}
static size_t deckBPcmRead(uint8_t* out, size_t maxLen) {
  size_t avail = deckBPcmUsed();
  size_t want = maxLen < avail ? maxLen : avail;
  if (want == 0) return 0;
  size_t firstPart = DECKB_PCM_RING_SIZE - deckBPcmTail;
  if (firstPart > want) firstPart = want;
  memcpy(out, deckBPcmRing + deckBPcmTail, firstPart);
  if (want > firstPart) memcpy(out + firstPart, deckBPcmRing, want - firstPart);
  deckBPcmTail = (deckBPcmTail + want) % DECKB_PCM_RING_SIZE;
  return want;
}

// 照抄 device/puck_mixer.py::crossfade_gains() 的邏輯（線性 crossfade），純 C 重寫，
// 行為要跟 Python 版一致，方便日後對照除錯。elapsedS<=0→(1,0)；elapsedS>=durationS→
// (0,1)；durationS<=0→立即切到 b。
static void crossfadeGains(float elapsedS, float durationS, float* gainA, float* gainB) {
  if (durationS <= 0.0f) { *gainA = 0.0f; *gainB = 1.0f; return; }
  float frac = elapsedS / durationS;
  if (frac < 0.0f) frac = 0.0f;
  if (frac > 1.0f) frac = 1.0f;
  *gainA = 1.0f - frac;
  *gainB = frac;
}
#endif  // STEP >= 10

// 2026-07-26：/audio_stream 現在送 MP3，ring buffer 裡裝的是壓縮 bytes——消費端不能再
// 直接 i2s_write，要先解碼。用 pschatzmann/arduino-libhelix 的 MP3DecoderHelix：
// decode-only（不像 ESP32-audioI2S 整包接管連線），塞 bytes 進去、解碼完的 PCM 經
// callback 吐回來，剛好對得上這裡「網路/解碼播放分離」的既有雙 task 架構——
// audioNetworkTask 完全不用改，一樣只管把 bytes 塞進 ring buffer。
using namespace libhelix;

static void mp3DataCallback(MP3FrameInfo &info, short *pcm_buffer, size_t len, void *ref);
static MP3DecoderHelix mp3Decoder(mp3DataCallback);
static int mp3LastRate = -1;
static int mp3LastChans = -1;

// 解碼出的 PCM 經這裡直接 i2s_write（write() 內部同步呼叫，跟舊版直接 i2s_write 的
// 位置等價，不是額外一層排隊）。samprate/nChans 跟上次不同才重設 I2S 時脈——正常情況
// server 端 bitrate/rate 是固定的，這個分支只在第一個 frame 解出來時觸發一次。
static void mp3DataCallback(MP3FrameInfo &info, short *pcm_buffer, size_t len, void *ref) {
  if (len == 0) return;
  if (info.samprate != mp3LastRate || info.nChans != mp3LastChans) {
    i2s_set_clk(I2S_SPK_PORT, info.samprate, I2S_BITS_PER_SAMPLE_16BIT,
                info.nChans == 1 ? I2S_CHANNEL_MONO : I2S_CHANNEL_STEREO);
    mp3LastRate = info.samprate;
    mp3LastChans = info.nChans;
    Serial.printf("[Stream] MP3 格式：%dHz %dch\n", info.samprate, info.nChans);
  }
#if STEP >= 10
  // STEP 10：crossfade 測試視窗內，把 deck B 的 PCM 依 gain 疊加進來——deck A 來源
  //本身沒換（仍是 /audio_stream），純測混音數學/CPU負擔，見檔頭 STEP 10 說明。
  if (crossfadeActive) {
    static int16_t mixBuf[4096];
    size_t nSamples = len;
    if (nSamples > sizeof(mixBuf) / sizeof(mixBuf[0])) nSamples = sizeof(mixBuf) / sizeof(mixBuf[0]);
    size_t gotBytes = deckBPcmRead((uint8_t*)mixBuf, nSamples * sizeof(int16_t));
    size_t gotSamples = gotBytes / sizeof(int16_t);

    uint32_t elapsedMs = millis() - crossfadeStartMs;
    float gainA, gainB;
    crossfadeGains(elapsedMs / 1000.0f, crossfadeDurationMs / 1000.0f, &gainA, &gainB);

    for (size_t i = 0; i < nSamples; i++) {
      float a = pcm_buffer[i] * gainA;
      float b = (i < gotSamples) ? mixBuf[i] * gainB : 0.0f;
      float mixed = a + b;
      if (mixed > 32767.0f) mixed = 32767.0f;
      if (mixed < -32768.0f) mixed = -32768.0f;
      pcm_buffer[i] = (int16_t)mixed;
    }
    if (elapsedMs >= crossfadeDurationMs) {
      crossfadeActive = false;   // 這一刀不做「切成deck B變主線」，測試視窗跑完就收手
      Serial.println("[Mix] crossfade 測試視窗結束");
    }
  }
#endif
  size_t written;
  i2s_write(I2S_SPK_PORT, pcm_buffer, len * sizeof(short), &written, portMAX_DELAY);
#if STREAM_DEBUG_PRINT
  static uint32_t _dbgCounter = 0;
  if (++_dbgCounter % 40 == 0) {   // 128kbps下每個MP3 frame ~1152 samples/26ms，~1s印一次
    int16_t peak = 0;
    for (size_t i = 0; i < len; i++) {
      int16_t a = pcm_buffer[i] < 0 ? (int16_t)-pcm_buffer[i] : pcm_buffer[i];
      if (a > peak) peak = a;
    }
    Serial.printf("[Stream] decode#%u samples=%u peak=%d ringUsed=%u\n",
                  (unsigned)_dbgCounter, (unsigned)len, (int)peak, (unsigned)ringUsed());
  }
#endif
}

// 消費端：只管從 ring buffer 拿壓縮 bytes 餵 MP3 解碼器，不碰網路，WiFi 抖動完全感受
// 不到（buffer 存量夠撐過去）。開播前先蓄到 STREAM_PREBUF_BYTES（模擬串流 app 的
// 「緩衝中」畫面），真的斷貨（ring 見底）就靜音、回頭重新蓄一輪，避免忽有忽無的破音。
void audioPlaybackTask(void* pv) {
  Serial.println("[Stream] audioPlaybackTask 已啟動");
  mp3Decoder.begin();
  uint8_t buf[512];
  for (;;) {
    if (mp3NeedsReset) {
      // 只從這裡（consumer 自己）動 ringTail，追上 ringHead 當下值＝丟棄重連前的舊
      // 殘留資料，不動 network task 的 ringHead，維持 single-producer/single-consumer
      // 約定。decoder 一起重置，避免舊連線尾巴的殘留 bytes 跟新連線開頭錯位。
      ringTail = ringHead;
      mp3Decoder.begin();
      mp3NeedsReset = false;
    }
    if (!streamPrimed) {
      if (ringUsed() >= STREAM_PREBUF_BYTES) {
        streamPrimed = true;
        Serial.println("[Stream] buffer 蓄滿，開始播放");
      } else {
        i2s_zero_dma_buffer(I2S_SPK_PORT);
        vTaskDelay(pdMS_TO_TICKS(20));
        continue;
      }
    }
    size_t avail = ringUsed();
    if (avail == 0) {
      // 只是瞬間見底（網路正常、只是這一刻還沒補到）→ 靜音這一輪就好，不強制整個重蓄
      // buffer。2026-07-25 實測：這裡一見底就 streamPrimed=false，逼下一輪重蓄到
      // STREAM_PREBUF_BYTES 才准播，稍有抖動就整段重來一次，聽起來變成規律性卡頓、
      // 一直印「buffer 蓄滿」。真正的斷線重蓄門檻交給 audioNetworkTask 重連時設。
      i2s_zero_dma_buffer(I2S_SPK_PORT);
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    size_t want = avail < sizeof(buf) ? avail : sizeof(buf);
    size_t n = ringRead(buf, want);
    mp3Decoder.write(buf, n);   // 解碼出的 PCM 經 mp3DataCallback 同步 i2s_write
  }
}

// STEP 7：車載 present 心跳（POST /car）——伺服器 TTL 預設 90s，這裡每 30s 送一次留足
// margin。斷電＝板子直接死掉、心跳自然停送，交給伺服器 TTL 收尾（不用板子主動告知斷電）。
//
// 2026-07-25 除錯記錄（別再重踩，方案 A 已落地）：
//   ①一開始懷疑 loopTask（跑 postAudio 的 TLS handshake）堆疊 8KB 不夠——實機崩潰前
//     堆疊水位完全健康、且崩潰當下沒有任何 PTT/TLS 活動，排除。
//   ②改猜「兩顆不同 core 同時碰 lwIP」，把心跳釘死去 audioNetworkTask 同一顆 core
//     （carHeartbeatTask，仍保留這個安排）——30 分鐘實機蹲點照樣崩 6 次，排除。
//     blocking I/O 呼叫本身就會 yield 讓出 CPU，同 core 不等於真正互斥執行。
//   ③試過窄範圍互斥鎖，但鎖住了 `readStringUntil()`/`http.POST()` 這種可能阻塞到
//     client 逾時（20s）的呼叫——持鎖阻塞 20 秒，17-20 秒內就撞 task watchdog，
//     比原本崩潰還快，撤掉。
//   ④隔離實驗：整個拔掉心跳任務，只留 audioNetworkTask+audioPlaybackTask，
//     31 分鐘零崩潰——證實心跳的 connect/POST/close 週期是必要條件。
//   ⑤方案 A（目前這版）：不用 HTTPClient（整包 POST 是不可拆的黑盒），改手動
//     WiFiClient + lockedReadLine（見 audioNetworkTask 前的定義）——鎖只包住
//     connect()／print() 這種 LAN 上通常幾十毫秒內完成的短操作，讀取一律先
//     available() 非阻塞判斷，沒資料就不鎖、不會有③那種持鎖空等的情況。
// 手動 HTTP POST（沿用 lockedReadLine 的鎖範圍紀律）：鎖只包住 connect()／print()／
// stop() 這種短操作，等回應期間不持鎖。回傳 HTTP 狀態碼，connect 失敗回 -1。
// client 傳 WiFiClient（區網明碼）或 WiFiClientSecure（Funnel TLS，setInsecure() 後傳入）
// 都可以——WiFiClientSecure 是 WiFiClient 的子類別。
static int postHttp(WiFiClient& client, const char* host, uint16_t port, int32_t connectTimeoutMs,
                     const char* path, const char* contentType, const uint8_t* body, size_t bodyLen,
                     uint32_t readTimeoutMs) {
  LWIP_LOCK();
  bool connectOk = client.connect(host, port, connectTimeoutMs);
  LWIP_UNLOCK();
  if (!connectOk) return -1;
  // WiFiClientSecure.available() 底層會真的等 socket recv()（見 audioNetworkTask 前的
  // 2026-07-26 註解，available() 已經一起上鎖，這裡只是控制輪詢節奏）。
  client.setTimeout(500);

  String header = String("POST ") + path + "?t=" + MARVIN_TOKEN + " HTTP/1.1\r\n" +
                  "Host: " + host + "\r\n" +
                  "Content-Type: " + contentType + "\r\n" +
                  "Content-Length: " + bodyLen + "\r\n" +
                  "Connection: close\r\n\r\n";
  LWIP_LOCK();
  client.print(header);
  client.write(body, bodyLen);
  LWIP_UNLOCK();

  String status;
  lockedReadLine(client, status, readTimeoutMs);   // 只在乎狀態碼，不解析/等 body
  int code = 0;
  int sp1 = status.indexOf(' ');
  if (sp1 > 0) code = status.substring(sp1 + 1, sp1 + 4).toInt();
  LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
  return code;
}

// STEP 8a：手動 GET + 讀 body（跟 postHttp 對稱、同款鎖紀律；body 走 Connection: close
// 讀到斷線為止，不解析 Content-Length——/car_commands 回應很小，這樣最簡單）。
// bodyBuf 由呼叫端提供（ps_malloc，避免佔 stack），塞不下就截斷（回傳值 = 實際讀到的
// bytes，可能等於 bodyBufSize）。回傳 HTTP 狀態碼，connect 失敗回 -1。
static int getHttpBody(WiFiClient& client, const char* host, uint16_t port, int32_t connectTimeoutMs,
                        const char* path, uint32_t readTimeoutMs,
                        uint8_t* bodyBuf, size_t bodyBufSize, size_t* outBodyLen) {
  *outBodyLen = 0;
  LWIP_LOCK();
  bool connectOk = client.connect(host, port, connectTimeoutMs);
  LWIP_UNLOCK();
  if (!connectOk) return -1;
  client.setTimeout(500);

  String req = String("GET ") + path + " HTTP/1.1\r\n" +
               "Host: " + host + "\r\nConnection: close\r\n\r\n";
  LWIP_LOCK(); client.print(req); LWIP_UNLOCK();

  String status;
  lockedReadLine(client, status, readTimeoutMs);
  int code = 0;
  int sp1 = status.indexOf(' ');
  if (sp1 > 0) code = status.substring(sp1 + 1, sp1 + 4).toInt();

  // 跳過 headers，讀到空行＝標頭結束。⚠️ 2026-08-11 實機踩到：外層不能用
  // `while (client.connected())` 當守門（audioNetworkTask 的 chunked 長連線那份可以，
  // 因為連線持續開著）——/car_commands 是 Connection: close 的短回應，本地區網幾乎
  // 瞬間送完+關閉，ESP32 使用者層跑到這行時 connected() 常常已經回 false（即使 socket
  // buffer 裡還有沒讀完的 header/body），outer while 直接 0 次跳過、標頭全部漏進
  // body buffer（實測：body 開頭印出一堆 Access-Control-*/Content-Type header 行）。
  // 改成純靠 lockedReadLine() 自己的回傳值收尾——它內部先查 available() 才查
  // connected()，能正確吃到「已斷線但還有緩衝資料」這種情況。
  for (;;) {
    String h;
    if (!lockedReadLine(client, h, readTimeoutMs)) break;
    if (h.length() == 0) break;
  }
  size_t got = 0;
  while (got < bodyBufSize) {
    size_t n = lockedReadBytes(client, bodyBuf + got, bodyBufSize - got, readTimeoutMs);
    if (n == 0) break;                  // 逾時空讀或斷線＝body 讀完了
    got += n;
  }
  *outBodyLen = got;
  LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
  return code;
}

// STEP 8a：抽 JSON 最外層 "seq":<N>——回應固定是 {"seq":N,"commands":[...]}，"seq" 這個
// key 第一次出現的位置一定是最外層那個（commands 陣列裡每個指令物件的 "seq" 排在它
// 後面），用第一次出現位置抽數字即可，不需要完整 JSON parser。指令內容（cmd/url/
// duration_s）留到下一刀真的要套用到 deck 邏輯時再解析，這一刀只求「seq 有沒有往前
// 推進、能不能連上」。
static uint32_t parseTopLevelSeq(const uint8_t* body, size_t len) {
  const char* key = "\"seq\":";
  size_t klen = strlen(key);
  for (size_t i = 0; i + klen < len; i++) {
    if (memcmp(body + i, key, klen) == 0) {
      return (uint32_t)strtoul((const char*)(body + i + klen), nullptr, 10);
    }
  }
  return 0;
}

#define CMD_POLL_BODY_MAX 2048
static uint8_t* cmdPollBodyBuf = nullptr;
static uint32_t lastCmdSeq = 0;

// STEP 8a：每 1s 輪詢一次 /car_commands，收到新指令只 log、不套用（edge端混音第一刀，
// 見檔頭 STEP 8a 說明）。跟 carHeartbeatTask 同款區網優先／Funnel 回退、同款
// LWIP_LOCK 紀律，釘同一顆 core（0），優先權跟心跳一樣低（1）——這是低頻輪詢，不搶
// audioNetworkTask/audioPlaybackTask 的 CPU。
void commandPollTask(void* pv) {
  Serial.println("[CmdPoll] commandPollTask 已啟動（STEP 8a：只 log，不套用指令）");
  cmdPollBodyBuf = (uint8_t*)ps_malloc(CMD_POLL_BODY_MAX);
  if (cmdPollBodyBuf == nullptr) {
    Serial.println("[CmdPoll] ❌ ps_malloc 失敗，指令輪詢停用");
    vTaskDelete(nullptr);
    return;
  }
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    if (WiFi.status() != WL_CONNECTED) continue;

    char path[96];
    snprintf(path, sizeof(path), "/car_commands?since=%u&t=%s", (unsigned)lastCmdSeq, MARVIN_TOKEN);

    size_t bodyLen = 0;
    WiFiClient localClient;
    int code = getHttpBody(localClient, MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 1200,
                            path, 3000, cmdPollBodyBuf, CMD_POLL_BODY_MAX, &bodyLen);
    if (code <= 0) {
      WiFiClientSecure funnelClient; funnelClient.setInsecure();
      funnelClient.setHandshakeTimeout(5);
      code = getHttpBody(funnelClient, MARVIN_HOST, MARVIN_PORT, 5000,
                          path, 5000, cmdPollBodyBuf, CMD_POLL_BODY_MAX, &bodyLen);
    }
    if (code != 200 || bodyLen == 0) continue;

    uint32_t newSeq = parseTopLevelSeq(cmdPollBodyBuf, bodyLen);
    if (newSeq > lastCmdSeq) {
      Serial.printf("[CmdPoll] 新指令 seq %u→%u：%.*s\n",
                    (unsigned)lastCmdSeq, (unsigned)newSeq, (int)bodyLen, (const char*)cmdPollBodyBuf);
      lastCmdSeq = newSeq;
#if STEP >= 9
      dispatchNewCommands(cmdPollBodyBuf, bodyLen);
#endif
    }
  }
}

#if STEP >= 9
// ============================================================
// STEP 9：Deck B —— 獨立第二條 network+decode pipeline（見檔頭說明）。跟主 deck（STEP7
// 的 streamRing/mp3Decoder 那組）完全分開的一組全域狀態，零共用，確保這一刀就算整個
// 崩潰/卡死也不會拖累既有 /audio_stream 播放（deckB 的 task 掛了頂多 deck B 沒聲音統計、
// 主播放不受影響）。目前解碼出的 PCM 只做峰值統計 log，不接 i2s_write。
// ============================================================
#define DECKB_RING_SIZE (256 * 1024)
static uint8_t* deckBRing = nullptr;
static volatile size_t deckBHead = 0, deckBTail = 0;
static volatile bool deckBActive = false;       // queue_next 開、stop 關
static volatile bool deckBNeedsReset = false;   // 換URL/重連：跟主deck mp3NeedsReset同款做法
static char deckBUrl[256] = {0};
static SemaphoreHandle_t deckBUrlMutex = nullptr;   // 保護 deckBUrl（commandPollTask寫、deckBNetworkTask讀）

static inline size_t deckBUsed() {
  size_t h = deckBHead, t = deckBTail;
  return (h >= t) ? (h - t) : (DECKB_RING_SIZE - t + h);
}
static inline size_t deckBFree() { return DECKB_RING_SIZE - 1 - deckBUsed(); }

// 跟主 deck 的 ringWrite/ringRead 同款 memcpy 版實作，理由見那兩個函式前的註解。
static void deckBRingWrite(const uint8_t* data, size_t len) {
  size_t offset = 0;
  while (offset < len) {
    while (deckBFree() == 0) {
      if (!deckBActive) return;   // 被 stop 掉了，不要在這裡卡死等 free
      vTaskDelay(pdMS_TO_TICKS(1));
    }
    size_t chunk = len - offset;
    size_t freeNow = deckBFree();
    if (chunk > freeNow) chunk = freeNow;
    size_t firstPart = DECKB_RING_SIZE - deckBHead;
    if (firstPart > chunk) firstPart = chunk;
    memcpy(deckBRing + deckBHead, data + offset, firstPart);
    if (chunk > firstPart) memcpy(deckBRing, data + offset + firstPart, chunk - firstPart);
    deckBHead = (deckBHead + chunk) % DECKB_RING_SIZE;
    offset += chunk;
  }
}
static size_t deckBRingRead(uint8_t* out, size_t maxLen) {
  size_t avail = deckBUsed();
  size_t want = maxLen < avail ? maxLen : avail;
  if (want == 0) return 0;
  size_t firstPart = DECKB_RING_SIZE - deckBTail;
  if (firstPart > want) firstPart = want;
  memcpy(out, deckBRing + deckBTail, firstPart);
  if (want > firstPart) memcpy(out + firstPart, deckBRing, want - firstPart);
  deckBTail = (deckBTail + want) % DECKB_RING_SIZE;
  return want;
}

// deck B 的 MP3 解碼 callback：只統計峰值+frame數，不寫 i2s（這一刀不接主輸出）。
static void mp3DataCallbackB(MP3FrameInfo &info, short *pcm_buffer, size_t len, void *ref);
static MP3DecoderHelix mp3DecoderB(mp3DataCallbackB);
static uint32_t deckBFrameCount = 0;
static int16_t deckBPeak = 0;

static void mp3DataCallbackB(MP3FrameInfo &info, short *pcm_buffer, size_t len, void *ref) {
  if (len == 0) return;
#if STEP >= 10
  deckBPcmWrite((const uint8_t*)pcm_buffer, len * sizeof(int16_t));
#endif
  deckBFrameCount++;
  for (size_t i = 0; i < len; i++) {
    int16_t a = pcm_buffer[i] < 0 ? (int16_t)-pcm_buffer[i] : pcm_buffer[i];
    if (a > deckBPeak) deckBPeak = a;
  }
  if (deckBFrameCount % 40 == 0) {   // 128kbps下每frame~26ms，~1s印一次
    Serial.printf("[DeckB] decode#%u %dHz %dch samples=%u peak=%d ringUsed=%u\n",
                  (unsigned)deckBFrameCount, info.samprate, info.nChans,
                  (unsigned)len, (int)deckBPeak, (unsigned)deckBUsed());
    deckBPeak = 0;
  }
}

// 跟主 deck audioNetworkTask 同款：手動解 chunked，區網優先/Funnel回退，LWIP_LOCK 紀律。
// 差異：deckBActive=false 時直接空轉（deck 沒開），不像主 deck 永遠常駐連線。
void deckBNetworkTask(void* pv) {
  Serial.println("[DeckB] deckBNetworkTask 已啟動（STEP 9：只統計，不接i2s）");
  for (;;) {
    if (!deckBActive) { vTaskDelay(pdMS_TO_TICKS(200)); continue; }
    if (WiFi.status() != WL_CONNECTED) { vTaskDelay(pdMS_TO_TICKS(500)); continue; }

    char urlLocal[256];
    xSemaphoreTake(deckBUrlMutex, portMAX_DELAY);
    strncpy(urlLocal, deckBUrl, sizeof(urlLocal) - 1);
    urlLocal[sizeof(urlLocal) - 1] = 0;
    xSemaphoreGive(deckBUrlMutex);
    if (urlLocal[0] == 0) { vTaskDelay(pdMS_TO_TICKS(200)); continue; }

    // ⚠️ url 沒做 URL-encode——/car_commands 目前只會塞乾淨的 youtube watch url（不含
    // 需要 encode 的字元），真撞到特殊字元的 url 再補，這一刀先不處理。
    char path[320];
    snprintf(path, sizeof(path), "/puck_deck?url=%s&t=%s", urlLocal, MARVIN_TOKEN);

    WiFiClient localClient;
    WiFiClientSecure funnelClient; funnelClient.setInsecure();
    funnelClient.setHandshakeTimeout(5);
    LWIP_LOCK();
    bool connectOk = localClient.connect(MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 1200);
    LWIP_UNLOCK();
    bool useFunnel = !connectOk;
    if (useFunnel) {
      LWIP_LOCK();
      connectOk = funnelClient.connect(MARVIN_HOST, MARVIN_PORT, 5000);
      LWIP_UNLOCK();
    }
    WiFiClient& client = useFunnel ? (WiFiClient&)funnelClient : localClient;
    if (!connectOk) {
      Serial.println("[DeckB] ⚠️ connect() 失敗（區網+Funnel都失敗），2s後重試");
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }
    const char* host = useFunnel ? MARVIN_HOST : MARVIN_LOCAL_HOST;
    client.setTimeout(500);
    String req = String("GET ") + path + " HTTP/1.1\r\nHost: " + host + "\r\nConnection: close\r\n\r\n";
    LWIP_LOCK(); client.print(req); LWIP_UNLOCK();

    String status;
    lockedReadLine(client, status, 20000);
    bool ok200 = status.indexOf("200") > 0;
    bool chunked = false;
    while (client.connected()) {   // /puck_deck 跟 /audio_stream 一樣是長連線 chunked
      String h;                    // stream，這裡沿用同款 connected() 守門沒問題（見
      if (!lockedReadLine(client, h, 20000)) break;   // getHttpBody 那個 bug 的註解：
      if (h.length() == 0) break;                      // 短 Connection:close 回應才會撞）
      if (h.indexOf("chunked") >= 0) chunked = true;
    }
    if (!ok200 || !chunked) {
      Serial.printf("[DeckB] /puck_deck 非預期回應（200=%d chunked=%d）：%s",
                    ok200, chunked, status.c_str());
      LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }
    Serial.printf("[DeckB] /puck_deck 連上：%s\n", urlLocal);
    deckBNeedsReset = true;

    uint8_t buf[4096];
    while (client.connected() && deckBActive) {
      String sizeLine;
      if (!lockedReadLine(client, sizeLine, 20000)) sizeLine = "";
      long chunkSize = strtol(sizeLine.c_str(), nullptr, 16);
      if (chunkSize <= 0) {
        Serial.println("[DeckB] chunk-size 異常，斷線重連");
        break;
      }
      long remain = chunkSize;
      while (remain > 0 && client.connected() && deckBActive) {
        size_t want = remain < (long)sizeof(buf) ? (size_t)remain : sizeof(buf);
        size_t n = lockedReadBytes(client, buf, want, 20000);
        if (n == 0) { vTaskDelay(pdMS_TO_TICKS(1)); continue; }
        deckBRingWrite(buf, n);
        remain -= (long)n;
      }
      { String _crlf; lockedReadLine(client, _crlf, 20000); }
    }
    LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
    Serial.println("[DeckB] /puck_deck 斷線");
    if (!deckBActive) {
      Serial.println("[DeckB] 已被 stop，等下一次 queue_next");
    } else {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }
}

void deckBPlaybackTask(void* pv) {
  Serial.println("[DeckB] deckBPlaybackTask 已啟動");
  mp3DecoderB.begin();
  uint8_t buf[512];
  for (;;) {
    if (!deckBActive) { vTaskDelay(pdMS_TO_TICKS(200)); continue; }
    if (deckBNeedsReset) {
      deckBTail = deckBHead;   // 只從 consumer 自己動 tail，理由同主 deck 的說明
      mp3DecoderB.begin();
      deckBNeedsReset = false;
    }
    size_t avail = deckBUsed();
    if (avail == 0) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }
    size_t want = avail < sizeof(buf) ? avail : sizeof(buf);
    size_t n = deckBRingRead(buf, want);
    mp3DecoderB.write(buf, n);   // 解碼出的 PCM 經 mp3DataCallbackB 統計，不寫i2s
  }
}

// STEP 9：抽單一 JSON 物件裡 "key":"value" 字串欄位，搜尋範圍限制在 [from,to) 避免抽到
// 相鄰下一個 command 物件的同名欄位。回傳擷取長度（0＝沒找到）。
// ⚠️ 2026-08-11 實機踩到：pattern 不能寫死「冒號後緊接引號」——aiohttp web.json_response
// 底層 json.dumps 預設分隔符是 ": "（冒號後有空格），寫死不含空格的 pattern 永遠比對
// 不到，第一版 STEP 9 因此整段 deckB 邏輯從沒被觸發過（log 全靜默、查了老半天才發現
// 不是連線問題，是這裡的字串比對問題）。改成 pattern 只到冒號為止，冒號後手動跳過
// 空格再找開頭引號。
static size_t extractJsonStringField(const uint8_t* body, size_t from, size_t to,
                                      const char* key, char* out, size_t outSize) {
  char pattern[32];
  snprintf(pattern, sizeof(pattern), "\"%s\":", key);
  size_t plen = strlen(pattern);
  for (size_t i = from; i + plen < to; i++) {
    if (memcmp(body + i, pattern, plen) != 0) continue;
    size_t p = i + plen;
    while (p < to && body[p] == ' ') p++;
    if (p >= to || body[p] != '"') continue;   // 撞到同名但非字串欄位，跳過找下一個
    size_t vstart = p + 1, vend = vstart;
    while (vend < to && body[vend] != '"') vend++;
    size_t vlen = vend - vstart;
    if (vlen >= outSize) vlen = outSize - 1;
    memcpy(out, body + vstart, vlen);
    out[vlen] = 0;
    return vlen;
  }
  out[0] = 0;
  return 0;
}

#if STEP >= 10
// STEP 10：抽單一 JSON 物件裡 "key": <number> 數字欄位（duration_s 用），同款空格容忍
// + 搜尋範圍限制，找不到回 false（out 不變，呼叫端自己決定 fallback）。
static bool extractJsonNumberField(const uint8_t* body, size_t from, size_t to,
                                    const char* key, float* out) {
  char pattern[32];
  snprintf(pattern, sizeof(pattern), "\"%s\":", key);
  size_t plen = strlen(pattern);
  for (size_t i = from; i + plen < to; i++) {
    if (memcmp(body + i, pattern, plen) != 0) continue;
    size_t p = i + plen;
    while (p < to && body[p] == ' ') p++;
    if (p >= to || (body[p] != '-' && (body[p] < '0' || body[p] > '9'))) continue;
    *out = strtof((const char*)(body + p), nullptr);
    return true;
  }
  return false;
}
#endif

// STEP 9：真的套用指令——queue_next（開 deck B）、stop（關 deck B）；STEP 10 起
// crossfade 也真的套用（算 gain 疊加，見 mp3DataCallback）。bodyLen 內可能不只一筆
// 指令，逐筆掃過 "cmd": 出現的位置套用（同款空格容忍，見上）。
void dispatchNewCommands(const uint8_t* body, size_t bodyLen) {
  const char* cmdKey = "\"cmd\":";
  size_t klen = strlen(cmdKey);
  for (size_t i = 0; i + klen < bodyLen; i++) {
    if (memcmp(body + i, cmdKey, klen) != 0) continue;
    size_t p = i + klen;
    while (p < bodyLen && body[p] == ' ') p++;
    if (p >= bodyLen || body[p] != '"') continue;
    size_t nameStart = p + 1, nameEnd = nameStart;
    while (nameEnd < bodyLen && body[nameEnd] != '"') nameEnd++;
    size_t nameLen = nameEnd - nameStart;

    if (nameLen == 10 && memcmp(body + nameStart, "queue_next", 10) == 0) {
      char url[256];
      size_t searchTo = bodyLen < i + 300 ? bodyLen : i + 300;
      if (extractJsonStringField(body, i, searchTo, "url", url, sizeof(url)) > 0) {
        xSemaphoreTake(deckBUrlMutex, portMAX_DELAY);
        strncpy(deckBUrl, url, sizeof(deckBUrl) - 1);
        deckBUrl[sizeof(deckBUrl) - 1] = 0;
        xSemaphoreGive(deckBUrlMutex);
        deckBActive = true;
#if STEP >= 10
        // 新歌換源：清掉舊歌可能殘留在 PCM ring 裡的樣本，避免下次 crossfade 混進
        // 上一首的尾巴。crossfadeActive 也一併關掉——舊的 crossfade 視窗不該延續到
        // 新歌上。
        crossfadeActive = false;
        deckBPcmHead = 0; deckBPcmTail = 0;
#endif
        Serial.printf("[DeckB] queue_next → %s\n", url);
      }
    } else if (nameLen == 4 && memcmp(body + nameStart, "stop", 4) == 0) {
      deckBActive = false;
#if STEP >= 10
      crossfadeActive = false;
      deckBPcmHead = 0; deckBPcmTail = 0;
#endif
      Serial.println("[DeckB] stop → deck B 停用");
#if STEP >= 10
    } else if (nameLen == 9 && memcmp(body + nameStart, "crossfade", 9) == 0) {
      float durationS = 4.0f;   // 找不到 duration_s 欄位就用跟 Mac 端 PuckCommandQueue
                                 // 同款預設值（見 puck_command_queue.py::crossfade()）
      size_t searchTo = bodyLen < i + 300 ? bodyLen : i + 300;
      extractJsonNumberField(body, i, searchTo, "duration_s", &durationS);
      crossfadeStartMs = millis();
      crossfadeDurationMs = (uint32_t)(durationS * 1000.0f);
      crossfadeActive = true;
      Serial.printf("[Mix] crossfade → duration=%.1fs\n", durationS);
#endif
    }
  }
}
#endif  // STEP >= 9

void carHeartbeat() {
  // 先試區網明碼（快、家用WiFi成立）；連不到（出門）就退回 Funnel TLS。
  const char* body = "{\"state\":\"present\"}";
  WiFiClient localClient;
  int code = postHttp(localClient, MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 1200,
                       "/car", "application/json", (const uint8_t*)body, strlen(body), 3000);
  if (code <= 0) {
    WiFiClientSecure funnelClient; funnelClient.setInsecure();
    funnelClient.setHandshakeTimeout(5);   // 見 testFunnelNow() 前的註解：預設120s跟connect()逾時無關
    code = postHttp(funnelClient, MARVIN_HOST, MARVIN_PORT, 5000,
                     "/car", "application/json", (const uint8_t*)body, strlen(body), 5000);
    Serial.printf("[Car] present 心跳(Funnel) HTTP %d\n", code);
  } else {
    Serial.printf("[Car] present 心跳(區網) HTTP %d\n", code);
  }
  if (code <= 0) { Serial.println("[Car] ⚠️ 心跳區網+Funnel都失敗，跳過這輪"); return; }
  // 診斷用堆疊水位（見上方 2026-07-25 註解）：已排除堆疊溢位假說，繼續留著當健康度
  // 觀察——四顆任務裡任何一個持續下探都值得回頭查。
  Serial.printf("[StackWM] loopTask=%u carHeartbeat=%u audioNet=%u audioPlay=%u words\n",
                loopTaskHandle ? (unsigned)uxTaskGetStackHighWaterMark(loopTaskHandle) : 0,
                (unsigned)uxTaskGetStackHighWaterMark(NULL),
                audioNetTaskHandle ? (unsigned)uxTaskGetStackHighWaterMark(audioNetTaskHandle) : 0,
                audioPlayTaskHandle ? (unsigned)uxTaskGetStackHighWaterMark(audioPlayTaskHandle) : 0);
}

// 獨立任務，釘死 core 0（跟 audioNetworkTask 同核，理由見 carHeartbeat() 前的註解）。
// 開機立刻打一次（不等第一輪 30s），之後每 30s 一次。
void carHeartbeatTask(void* pv) {
  for (;;) {
    if (WiFi.status() == WL_CONNECTED) carHeartbeat();
    vTaskDelay(pdMS_TO_TICKS(30000));
  }
}

void postAudio(int nSamples);  // 前置宣告（hold-to-talk 放開時呼叫）

// hold-to-talk：按住 PTT 期間持續錄音、放開送出。長度自適應，
// 不像固定秒數會切掉長句或錄多餘環境音。每 loop 呼叫一次（非阻塞）。
void pttHoldToTalk() {
  static bool recording = false;
  static int  recCount = 0;
  bool pressed = (digitalRead(PIN_BTN_PTT) == LOW);

  if (pressed && !recording) {           // ▼ 按下：開錄
    recording = true; recCount = 0;
    i2s_zero_dma_buffer(I2S_MIC_PORT);    // 丟掉按下前 DMA 累積的舊音
    setLed(LED_LISTENING);                // 收聽中：藍燈
    Serial.println("[PTT] ▼ 按下，開始錄音（按住說話）");
  }

  if (recording) {
    // 把 DMA 裡已到的 frame 全撈進 buffer（>>16 對齊 int16，見開箱體檢）
    int32_t frame; size_t n;
    while (recCount < MAX_REC_SAMPLES &&
           i2s_read(I2S_MIC_PORT, &frame, sizeof(frame), &n, 0) == ESP_OK &&
           n == sizeof(frame)) {
      recBuf[recCount++] = (int16_t)(frame >> 16);
    }
    bool full = (recCount >= MAX_REC_SAMPLES);
    if (!pressed || full) {               // ▲ 放開 或 撞上限：送出
      recording = false;
      float secs = recCount / (float)SAMPLE_RATE;
      if (full) Serial.printf("[PTT] ■ 達上限 %ds，送出（%.1fs）\n", MAX_REC_SECONDS, secs);
      else      Serial.printf("[PTT] ▲ 放開，送出（%.1fs）\n", secs);
      if (recCount >= MIN_REC_SAMPLES) { setLed(LED_PLAYING); postAudio(recCount); }
      else { Serial.println("[PTT] 太短（手滑？），忽略不送"); setLed(LED_STANDBY); }
    }
  }
}

// STEP 5：把 recBuf 前 nSamples 個樣本包成 WAV，POST /audio
void postAudio(int nSamples) {
  const int dataBytes = nSamples * 2;
  const int wavBytes = 44 + dataBytes;
  uint8_t* wav = (uint8_t*)ps_malloc(wavBytes);
  // 極簡 WAV 標頭（16kHz mono 16-bit）
  auto wr32=[&](int o,uint32_t v){wav[o]=v;wav[o+1]=v>>8;wav[o+2]=v>>16;wav[o+3]=v>>24;};
  auto wr16=[&](int o,uint16_t v){wav[o]=v;wav[o+1]=v>>8;};
  memcpy(wav,"RIFF",4); wr32(4,36+dataBytes); memcpy(wav+8,"WAVE",4);
  memcpy(wav+12,"fmt ",4); wr32(16,16); wr16(20,1); wr16(22,1);
  wr32(24,SAMPLE_RATE); wr32(28,SAMPLE_RATE*2); wr16(32,2); wr16(34,16);
  memcpy(wav+36,"data",4); wr32(40,dataBytes);
  memcpy(wav+44, recBuf, dataBytes);

  // 2026-07-26：先試區網明碼直連 Mac（跟 /car 心跳、/audio_stream 一致，快、家用WiFi成立），
  // 連不到（出門用4G）就退回 Funnel TLS——Funnel 那邊之前 ingress 卡住已經在 Tailscale
  // 側修好（tailscale funnel reset 重新註冊），兩條路現在都通。改用 postHttp()（手動
  // socket + lockedReadLine）而非 HTTPClient：HTTPClient 的 POST() 是不可拆的黑盒，鎖
  // 只要包住它就等於持鎖空等到逾時——carHeartbeat() 上面的除錯記錄③已經實測過這個
  // 組合會在 17-20 秒內撞 task watchdog，不要重踩。
  WiFiClient localClient;
  int code = postHttp(localClient, MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 1200,
                       "/audio", "audio/wav", wav, wavBytes, 15000);
  if (code <= 0) {
    Serial.println("[POST /audio] 區網打不到，退回 Funnel...");
    WiFiClientSecure funnelClient; funnelClient.setInsecure();
    funnelClient.setHandshakeTimeout(5);   // 見 testFunnelNow() 前的註解：預設120s跟connect()逾時無關
    code = postHttp(funnelClient, MARVIN_HOST, MARVIN_PORT, 5000,
                     "/audio", "audio/wav", wav, wavBytes, 15000);
  }
  Serial.printf("[POST /audio] HTTP %d\n", code);
  free(wav);
  Serial.printf("[StackWM] loopTask after postAudio: %u words\n",
                (unsigned)uxTaskGetStackHighWaterMark(NULL));
  if (code != 200) { setLed(LED_STANDBY); return; }   // 沒送成功＝沒回覆要播，別卡在青燈
#if STEP >= 7
  // 車載常駐串流模式：/reply 已停用，回覆會自動從 audioStreamTask 的 /audio_stream 播出。
  setLed(LED_STANDBY);
#elif STEP >= 6
  pollAndPlayReply();   // 輪詢 /reply，收到就用 PCM5102 就地播放
#else
  Serial.println("[POST] 送出後，Mac 會轉錄+回覆；回覆音訊走 GET /reply（STEP<6，板子還不會自己播）");
#endif
}

// ------------------------------------------------------------------
void setup() {
  loopTaskHandle = xTaskGetCurrentTaskHandle();   // setup()/loop() 是同一顆 Arduino loopTask
  lwipMutex = xSemaphoreCreateMutex();            // 必須在 audioNet/carHeartbeat 任務起來前建好
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== Marvin car_puck bring-up ===");

  setLed(LED_BOOT);
  neopixelWrite(PIN_RGB, 40, 28, 0);   // 立刻亮黃：setup 期間 loop 還沒跑

  // STEP 1：PSRAM 檢查（驗你買對 N16R8）
  Serial.printf("[PSRAM] size = %u bytes（N16R8 應 ~8388608）\n", (unsigned)ESP.getPsramSize());
  if (ESP.getPsramSize() < 4*1024*1024)
    Serial.println("[PSRAM] ❌ 沒偵測到大 PSRAM！確認買的是 N16R8 + Arduino 有開 PSRAM");
  recBuf = (int16_t*)ps_malloc(MAX_REC_SAMPLES * sizeof(int16_t));

  connectWiFi();

  pinMode(PIN_BTN_PTT, INPUT_PULLUP);
  pinMode(PIN_BTN_VOLUP, INPUT_PULLUP);
  pinMode(PIN_BTN_VOLDN, INPUT_PULLUP);

#if STEP >= 2
  testFunnelNow();
#endif
#if STEP >= 4
  startMic();
#endif
#if STEP >= 6
  startSpeaker();
#endif
#if STEP >= 7
  streamRing = (uint8_t*)ps_malloc(STREAM_RING_SIZE);
  Serial.printf("[Stream] streamRing ps_malloc %s（%u bytes）\n",
                streamRing ? "成功" : "❌失敗", (unsigned)STREAM_RING_SIZE);
  // 2026-07-25 修正：network 優先權要 >= playback，不是反過來。playback 本來就靠
  // i2s_write(...,portMAX_DELAY) 被 DMA 硬體節奏卡住、天然限速，不需要搶 CPU；
  // 反而是 network 要追一個真即時的來源（伺服器批次送 100ms 一包），優先權太低會被
  // playback 一直搶走 CPU 時間片，追不上進度，ring buffer 就一直在 0 附近打轉、
  // 聽起來斷斷續續（2026-07-25 實測：ringUsed 反覆见底又跳回 13-14KB，符合追不上批次
  // 送達節奏的症狀）。
  // 2026-07-25 再修：光調優先權只是把問題換方向——兩個任務仍同釘 core 0，network
  // 優先權較高時換成它偶爾長時間佔用 core 0（連線/重連時的一段同步處理）反過來餓死
  // playback，即使 ring buffer 存量健康（40-95 萬 bytes，遠高於 96KB 開播門檻）也一樣
  // 卡（STREAM_DEBUG_PRINT 實測：maxGap 常態 100-150ms，兩次分別量到 1623ms/2322ms 的
  // 停頓，同時 ringUsed 完全沒見底，排除資料不足，鎖定是 CPU 排程）。改把 playback 挪去
  // 跟 Arduino loop()/PTT 共用的 core 1（2026-07-25 之後心跳搬去 core 0，見
  // carHeartbeatTask）——loop() 剩下的工作量很輕，不會像 network task 一樣搶爆；
  // network 繼續獨占 core 0，兩者不再共核競爭，優先權高低就不重要了。
  // 優先權=2（>Arduino loopTask 預設的 1）：跟 loopTask 同核心、同優先權時 FreeRTOS 靠
  // 時間片輪流，playback 不保證每輪都搶到；loop() 裡的 PTT/心跳未來變重時可能偶爾晚一輪
  // 才輪到 playback。實測目前很穩（maxGap 10.7ms）才調的，不是修急迫症狀，是把「贏
  // loopTask」從機率變保證。
  BaseType_t _rNet = xTaskCreatePinnedToCore(audioNetworkTask, "audioNet", 16384, nullptr, 2, &audioNetTaskHandle, 0);
  BaseType_t _rPlay = xTaskCreatePinnedToCore(audioPlaybackTask, "audioPlay", 8192, nullptr, 2, &audioPlayTaskHandle, 1);
  Serial.printf("[Stream] 任務建立 net=%d play=%d（1=成功）\n", (int)_rNet, (int)_rPlay);
  // 心跳釘死 core 0（跟 audioNetworkTask 同核，理由見 carHeartbeat() 前的註解）；
  // 任務起來後第一輪迴圈立刻打一次，不用等第一輪 30s。隔離實驗（31 分鐘拔掉心跳
  // 零崩潰）已證實心跳是必要條件，方案 A 落地後重新啟用測試。
  xTaskCreatePinnedToCore(carHeartbeatTask, "carHeartbeat", 8192, nullptr, 1, nullptr, 0);
#endif
#if STEP >= 8
  // STEP 8a：指令輪詢，跟心跳同核心同優先權（低頻、不搶 audioNet/audioPlay 的 CPU）。
  xTaskCreatePinnedToCore(commandPollTask, "cmdPoll", 8192, nullptr, 1, nullptr, 0);
#endif
#if STEP >= 9
  // STEP 9：deck B —— 優先權都給 1（比主 deck 的 audioNet/audioPlay 的 2 低一階），
  // 這一刀是「順便驗證能不能撐」，不該搶主播放的 CPU/網路優先權。network 跟主 deck
  // 一樣釘 core 0（I/O bound），decode 跟主 deck 一樣釘 core 1（CPU bound，這一刀最想
  // 觀察的就是這裡會不會餓死 audioPlaybackTask）。
  deckBRing = (uint8_t*)ps_malloc(DECKB_RING_SIZE);
  deckBUrlMutex = xSemaphoreCreateMutex();
  Serial.printf("[DeckB] deckBRing ps_malloc %s（%u bytes）free heap=%u\n",
                deckBRing ? "成功" : "❌失敗", (unsigned)DECKB_RING_SIZE, (unsigned)ESP.getFreeHeap());
#if STEP >= 10
  // STEP 10：混音 PCM ring，要在 deckB 任務起來、真的開始解碼寫入前分配好。
  deckBPcmRing = (uint8_t*)ps_malloc(DECKB_PCM_RING_SIZE);
  Serial.printf("[Mix] deckBPcmRing ps_malloc %s（%u bytes）free heap=%u\n",
                deckBPcmRing ? "成功" : "❌失敗", (unsigned)DECKB_PCM_RING_SIZE, (unsigned)ESP.getFreeHeap());
#endif
  xTaskCreatePinnedToCore(deckBNetworkTask, "deckBNet", 16384, nullptr, 1, nullptr, 0);
  xTaskCreatePinnedToCore(deckBPlaybackTask, "deckBPlay", 8192, nullptr, 1, nullptr, 1);
#endif
  // 收尾定燈：WiFi 通但沒跑 Funnel 檢查（STEP 1）也給個 connected 提示；
  // 若前面已 setLed(ERROR/CONNECTED) 則不覆蓋。
  if (WiFi.status() == WL_CONNECTED && ledState == LED_BOOT) setLed(LED_CONNECTED);
  Serial.printf("[READY] STEP=%d\n", STEP);
}

void loop() {
#if STEP == 3 || STEP == 4
  // STEP 3/4 只驗按鍵通不通（印一下）。STEP 5 的 PTT 改走 hold-to-talk，
  // 不在這印（那個 delay 會打斷錄音節奏）。
  if (digitalRead(PIN_BTN_PTT)  == LOW) { Serial.println("[BTN] PTT 按下"); delay(150); }
  if (digitalRead(PIN_BTN_VOLUP)== LOW) { Serial.println("[BTN] Vol+"); delay(150); }
  if (digitalRead(PIN_BTN_VOLDN)== LOW) { Serial.println("[BTN] Vol-"); delay(150); }
#endif

#if STEP >= 5
  pttHoldToTalk();   // 按住說話、放開送出
#endif
  // 心跳 2026-07-25 起改由 carHeartbeatTask（core 0 專用任務）驅動，不再從這裡呼叫。
  updateLed();       // 狀態燈動畫（非阻塞）
  delay(5);
}
