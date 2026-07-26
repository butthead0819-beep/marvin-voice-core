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
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <driver/i2s.h>
#include <freertos/semphr.h>
#include <string.h>

// ========== 你要填的 ==========
#define STEP 7   // ← 從 1 開始，每步綠了再 +1

// 2026-07-25 懷疑：串流 debug 用的 Serial.printf 本身在 HWCDC 底下可能阻塞等 USB
// buffer（檔頭已知怪癖），會製造出我們正在追的那種週期性卡頓。先關掉排除，需要時開。
#define STREAM_DEBUG_PRINT 0

const char* WIFI_SSID    = "你的手機熱點名稱";
const char* WIFI_PASS    = "熱點密碼";
const char* MARVIN_HOST  = "macbook-air.tail7ba8d0.ts.net";   // 不含 https://
const int   MARVIN_PORT  = 443;
const char* MARVIN_TOKEN = "PASTE_YOUR_TOKEN";                // ⚠️ 別 commit 真 token

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
  Serial.printf("[WiFi] 連線 %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300); Serial.print("."); updateLed();   // 連線期間 setup 阻塞，靠這裡讓黃燈慢閃
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] OK, IP=%s RSSI=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
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
#define STREAM_RING_SIZE (1024 * 1024)
#define STREAM_PREBUF_BYTES (96 * 1000)
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
static bool lockedReadLine(WiFiClient& client, String& out, uint32_t timeoutMs) {
  out = "";
  uint32_t t0 = millis();
  for (;;) {
    if (client.available() > 0) {
      LWIP_LOCK();
      int c = client.read();
      LWIP_UNLOCK();
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
    int avail = client.available();
    if (avail > 0) {
      size_t chunk = (size_t)avail < (want - got) ? (size_t)avail : (want - got);
      LWIP_LOCK();
      int n = client.read(buf + got, chunk);
      LWIP_UNLOCK();
      if (n > 0) { got += (size_t)n; t0 = millis(); continue; }
    }
    if (!client.connected()) break;
    if (timeoutMs > 0 && millis() - t0 > timeoutMs) break;
    vTaskDelay(pdMS_TO_TICKS(1));
  }
  return got;
}
static volatile size_t ringHead = 0, ringTail = 0;   // 單一 producer（network）寫 head、單一 consumer（playback）讀 tail
static volatile bool streamPrimed = false;           // 是否已蓄滿過一輪，允許開始播放

static inline size_t ringUsed() {
  size_t h = ringHead, t = ringTail;
  return (h >= t) ? (h - t) : (STREAM_RING_SIZE - t + h);
}
static inline size_t ringFree() { return STREAM_RING_SIZE - 1 - ringUsed(); }

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
    // TEMP：明碼 WiFiClient 直連區網 IP（見上面 MARVIN_LOCAL_HOST 註解），跳過 TLS
    // 解密開銷，測試是否解決 throughput 上不去的問題。
    WiFiClient client;
    LWIP_LOCK();
    bool connectOk = client.connect(MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT);
    LWIP_UNLOCK();
    if (!connectOk) {
      Serial.println("[Stream] ⚠️ connect() 失敗，2s 後重試");
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }
    // mixer 是 on-demand 來源，兩幀之間偶爾會停超過 1s。舊版靠 client.setTimeout(20000)
    // 讓 Arduino 內建的 readStringUntil/readBytes 空等到 20s——2026-07-25 改用
    // lockedReadLine/lockedReadBytes（見上方定義），逾時改由呼叫端明講的 timeoutMs
    // 參數控制，setTimeout 已無作用、拿掉。
    String req = String("GET /audio_stream?t=") + MARVIN_TOKEN + " HTTP/1.1\r\n" +
                 "Host: " + MARVIN_LOCAL_HOST + "\r\nConnection: close\r\n\r\n";
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
    // 2026-07-25：原本這裡無條件 streamPrimed=false，代表每次重連（不管是真的斷貨還是
    // TCP 瞬斷/伺服器端小抖動）都強制暫停、重蓄滿 STREAM_PREBUF_BYTES 才准繼續播——即使
    // ring buffer 裡其實還有好幾秒沒播完的存量也照樣打斷，白白浪費掉上面拉大 buffer換來的
    // 抗抖動空間。改成只有「重連當下 ring 已經被榨乾」才需要重新蓄水；還有存量就讓
    // audioPlaybackTask 繼續吃舊資料撐過這次重連，聽不出斷點。
    if (ringUsed() < STREAM_PREBUF_BYTES) {
      streamPrimed = false;
    }
#if STREAM_DEBUG_PRINT
    else {
      // 2026-07-25：這行 Serial.printf 曾懷疑是「中斷變長」的兇手——HWCDC 底下
      // Serial.printf 已知偶爾會阻塞等 USB buffer（檔頭已知雷），剛好卡在重連後這個
      // 時間點，等於每次重連都多一次可能卡住的 print。關掉當預設，需要時再開驗證。
      Serial.printf("[Stream] 重連時 ring 仍有 %u bytes 存量，不重蓄、直接接續播放\n",
                    (unsigned)ringUsed());
    }
#endif

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

// 消費端：只管從 ring buffer 拿資料餵 I2S，不碰網路，WiFi 抖動完全感受不到
// （buffer 存量夠撐過去）。開播前先蓄到 STREAM_PREBUF_BYTES（模擬串流 app 的
// 「緩衝中」畫面），真的斷貨（ring 見底）就靜音、回頭重新蓄一輪，避免忽有忽無的破音。
void audioPlaybackTask(void* pv) {
  Serial.println("[Stream] audioPlaybackTask 已啟動");
  uint8_t buf[512];
  for (;;) {
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
    // 2026-07-25：逐次量測本輪迴圈間隔（micros），抓的是「單次卡住多久」而非平均速率——
    // 平均 KB/s 正常時完全可能藏著一次 20-30ms 的瞬間卡頓（佔一秒的一小部分，平均值看不
    // 出來），但耳朵聽得到。ring buffer 這次全程沒見底，代表破碎不是缺資料，是某個環節
    // 週期性卡住，這裡直接量測卡多久、卡在哪個環節。
    static uint32_t _lastIterUs = 0;
    static uint32_t _maxGapUs = 0;
    uint32_t nowUs = micros();
    if (_lastIterUs != 0) {
      uint32_t gap = nowUs - _lastIterUs;
      if (gap > _maxGapUs) _maxGapUs = gap;
    }
    _lastIterUs = nowUs;

    size_t want = avail < sizeof(buf) ? avail : sizeof(buf);
    size_t n = ringRead(buf, want);
    size_t written;
    esp_err_t werr = i2s_write(I2S_SPK_PORT, buf, n, &written, portMAX_DELAY);
    (void)werr;
#if STREAM_DEBUG_PRINT
    static uint32_t _dbgCounter = 0;
    if (++_dbgCounter % 80 == 0) {   // ~每 0.5s 印一次（80 次 * ~2.7ms/512bytes），別洗版
      int16_t peak = 0;
      for (size_t i = 0; i + 1 < n; i += 2) {
        int16_t s = (int16_t)(buf[i] | (buf[i + 1] << 8));
        int16_t a = s < 0 ? -s : s;
        if (a > peak) peak = a;
      }
      Serial.printf("[Stream] play#%u n=%u written=%u err=%d peak=%d ringUsed=%u maxGap=%.1fms\n",
                    (unsigned)_dbgCounter, (unsigned)n, (unsigned)written, (int)werr,
                    (int)peak, (unsigned)ringUsed(), _maxGapUs / 1000.0);
      _maxGapUs = 0;
    }
#endif
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
void carHeartbeat() {
  // TEMP（2026-07-25）：跟 /audio_stream 一致，明碼直連區網 IP，避免每 30s 一次的 TLS
  // 握手佔用同一顆 WiFi 天線的時間片，跟主串流搶頻寬造成瞬間 throughput 掉底。
  WiFiClient client;
  LWIP_LOCK();
  bool connectOk = client.connect(MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT);
  LWIP_UNLOCK();
  if (!connectOk) {
    Serial.println("[Car] ⚠️ 心跳 connect() 失敗，跳過這輪");
    return;
  }
  const char* body = "{\"state\":\"present\"}";
  String req = String("POST /car?t=") + MARVIN_TOKEN + " HTTP/1.1\r\n" +
               "Host: " + MARVIN_LOCAL_HOST + "\r\n" +
               "Content-Type: application/json\r\n" +
               "Content-Length: " + strlen(body) + "\r\n" +
               "Connection: close\r\n\r\n" + body;
  LWIP_LOCK(); client.print(req); LWIP_UNLOCK();

  String status;
  lockedReadLine(client, status, 3000);   // 只在乎狀態碼，不解析/等 body
  int code = 0;
  int sp1 = status.indexOf(' ');
  if (sp1 > 0) code = status.substring(sp1 + 1, sp1 + 4).toInt();
  Serial.printf("[Car] present 心跳 HTTP %d\n", code);
  LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
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

  // 2026-07-26：改走區網明碼直連 Mac（跟 /car 心跳、/audio_stream 一致），跳過 Funnel TLS
  // ——實測 Funnel 對 ESP32 的 TLS ClientHello 會在握手中回 EOF 直接斷線（lastError=
  // "SSL - The connection indicated an EOF"），/audio 送出必炸 HTTP -1。只在家測試網路有效；
  // 出門用 4G 時這條區網 IP 打不通，需要退回 Funnel（Funnel TLS 本身待查修）。
  // ⚠️ loopTask（core 1）跟 audioNetworkTask/carHeartbeatTask（core 0）共用 lwIP，未上鎖
  // 就從這裡直接 connect/POST 撞上就是已知的 pbuf_free 崩潰（實測重現：LoadProhibited
  // 當機重開機）。跟其他跨 core 的 lwIP 呼叫一致，全程包住 LWIP_LOCK/UNLOCK。
  HTTPClient http;
  WiFiClient client;
  String url = String("http://") + MARVIN_LOCAL_HOST + ":" + MARVIN_LOCAL_PORT + "/audio?t=" + MARVIN_TOKEN;
  LWIP_LOCK();
  http.begin(client, url);
  http.addHeader("Content-Type", "audio/wav");
  int code = http.POST(wav, wavBytes);
  String respBody = http.getString();
  http.end();
  LWIP_UNLOCK();
  Serial.printf("[POST /audio] HTTP %d：%s\n", code, respBody.c_str());
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
