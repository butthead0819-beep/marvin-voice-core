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

// ========== INMP441 I2S 麥克風腳位（V1.7 schematic P6；2026-07-17 錄音實測通過）==========
#define I2S_MIC_SCK   5     // SCK / BCLK
#define I2S_MIC_WS    4     // WS / LRCLK
#define I2S_MIC_SD    6     // SD / DATA（麥→ESP32）；L/R 接地=左聲道

// ========== MAX98357 喇叭 I2S 腳位（V1.7 schematic P7；PCM5102 並到同組）==========
// ⚠️ 這三根只有 schematic 依據，還沒接喇叭實測（PH2.0 喇叭未購入）。
#define I2S_AMP_BCLK  15
#define I2S_AMP_LRCLK 16
#define I2S_AMP_DIN   7
#define I2S_MIC_PORT  I2S_NUM_0

// ========== 錄音參數 ==========
#define SAMPLE_RATE   16000          // 16kHz mono 16-bit（STT 夠用、省 RAM）
#define REC_SECONDS   3
#define REC_SAMPLES   (SAMPLE_RATE * REC_SECONDS)

static int16_t* recBuf = nullptr;    // 放 PSRAM

// ------------------------------------------------------------------
void connectWiFi() {
  Serial.printf("[WiFi] 連線 %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300); Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf("[WiFi] OK, IP=%s RSSI=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  else
    Serial.println("[WiFi] ❌ 連不上，檢查 SSID/密碼/熱點開了沒");
}

// STEP 2：HTTPS GET /now?t=token —— 驗 Funnel 端到端 + TLS
void testFunnelNow() {
  WiFiClientSecure client;
  client.setInsecure();   // bring-up 先跳過憑證驗證（Funnel 是有效 LetsEncrypt，之後可加 CA）
  Serial.println("[HTTPS] 連 Funnel ...");
  if (!client.connect(MARVIN_HOST, MARVIN_PORT)) {
    Serial.println("[HTTPS] ❌ TLS 連不上（TLS 太重/沒網/Funnel 沒開）");
    return;
  }
  String req = String("GET /now?t=") + MARVIN_TOKEN + " HTTP/1.1\r\n" +
               "Host: " + MARVIN_HOST + "\r\nConnection: close\r\n\r\n";
  client.print(req);
  String statusLine = client.readStringUntil('\n');
  Serial.printf("[HTTPS] 回應：%s", statusLine.c_str());
  if (statusLine.indexOf("200") > 0)
    Serial.println("[HTTPS] ✅ 端到端通了！token 對、Funnel 對、TLS 沒問題");
  else if (statusLine.indexOf("401") > 0)
    Serial.println("[HTTPS] ⚠️ 401 = 通了但 token 錯，改 MARVIN_TOKEN");
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

// 錄 REC_SECONDS 秒到 recBuf（32-bit 讀入→右移成 16-bit）
void recordToBuf() {
  Serial.printf("[MIC] 錄音 %ds ...\n", REC_SECONDS);
  int32_t frame; size_t n; int got = 0;
  while (got < REC_SAMPLES) {
    i2s_read(I2S_MIC_PORT, &frame, sizeof(frame), &n, portMAX_DELAY);
    if (n == sizeof(frame)) recBuf[got++] = (int16_t)(frame >> 14);  // 24→16bit 概略
  }
  Serial.println("[MIC] 錄完");
}

// STEP 5：把 recBuf 包成 WAV，POST /audio
void postAudio() {
  const int dataBytes = REC_SAMPLES * 2;
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
  Serial.println("[POST] 送出後，Mac 會轉錄+回覆；回覆音訊走 GET /reply（有喇叭才聽得到）");
}

// ------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== Marvin car_puck bring-up ===");

  // STEP 1：PSRAM 檢查（驗你買對 N16R8）
  Serial.printf("[PSRAM] size = %u bytes（N16R8 應 ~8388608）\n", (unsigned)ESP.getPsramSize());
  if (ESP.getPsramSize() < 4*1024*1024)
    Serial.println("[PSRAM] ❌ 沒偵測到大 PSRAM！確認買的是 N16R8 + Arduino 有開 PSRAM");
  recBuf = (int16_t*)ps_malloc(REC_SAMPLES * sizeof(int16_t));

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
  Serial.printf("[READY] STEP=%d\n", STEP);
}

void loop() {
#if STEP >= 3
  if (digitalRead(PIN_BTN_PTT)  == LOW) { Serial.println("[BTN] PTT 按下"); delay(200); }
  if (digitalRead(PIN_BTN_VOLUP)== LOW) { Serial.println("[BTN] Vol+"); delay(200); }
  if (digitalRead(PIN_BTN_VOLDN)== LOW) { Serial.println("[BTN] Vol-"); delay(200); }
#endif

#if STEP >= 5
  if (digitalRead(PIN_BTN_PTT) == LOW) {
    recordToBuf();
    postAudio();
    delay(500);
  }
#endif
  delay(20);
}
