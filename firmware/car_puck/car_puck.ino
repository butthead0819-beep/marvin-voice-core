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

// ========== 你要填的 ==========
#define STEP 1   // ← 從 1 開始，每步綠了再 +1

const char* WIFI_SSID    = "你的手機熱點名稱";
const char* WIFI_PASS    = "熱點密碼";
const char* MARVIN_HOST  = "macbook-air.tail7ba8d0.ts.net";   // 不含 https://
const int   MARVIN_PORT  = 443;
const char* MARVIN_TOKEN = "PASTE_YOUR_TOKEN";                // ⚠️ 別 commit 真 token

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
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf("[WiFi] OK, IP=%s RSSI=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  else {
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
    Serial.println("[HTTPS] ❌ TLS 連不上（TLS 太重/沒網/Funnel 沒開）");
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
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,  // stereo
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 8,
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
      } else if (len > 0) {
        Serial.println("[SPK] ⚠️ ps_malloc 失敗，跳過播放");
      }
      String seqHdr = http.header("X-Reply-Seq");
      http.end();
      if (seqHdr.length()) lastReplySeq = (uint32_t)seqHdr.toInt();
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

  HTTPClient http;
  WiFiClientSecure client; client.setInsecure();
  String url = String("https://") + MARVIN_HOST + "/audio?t=" + MARVIN_TOKEN;
  http.begin(client, url);
  http.addHeader("Content-Type", "audio/wav");
  int code = http.POST(wav, wavBytes);
  Serial.printf("[POST /audio] HTTP %d：%s\n", code, http.getString().c_str());
  http.end(); free(wav);
  if (code != 200) { setLed(LED_STANDBY); return; }   // 沒送成功＝沒回覆要播，別卡在青燈
#if STEP >= 6
  pollAndPlayReply();   // 輪詢 /reply，收到就用 PCM5102 就地播放
#else
  Serial.println("[POST] 送出後，Mac 會轉錄+回覆；回覆音訊走 GET /reply（STEP<6，板子還不會自己播）");
#endif
}

// ------------------------------------------------------------------
void setup() {
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
  updateLed();       // 狀態燈動畫（非阻塞）
  delay(5);
}
