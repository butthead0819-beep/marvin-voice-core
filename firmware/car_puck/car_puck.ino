/*
 * car_puck.ino — Marvin ESP32-S3 車載 puck bring-up 骨架（Arduino）
 * 板：Goouuu ESP32-S3 N16R8 DevKit + S3 智能擴展板 V1.7
 *
 * 現行架構（2026-10 起 bring-up 的 STEP 漸進結構已移除，edge-mix STEP 8–10 隨伺服器端移除一併刪掉）：
 *   core 0：audioNetworkTask 常駐讀 GET /audio_stream（MP3 + ICY 曲名）→ PSRAM ring
 *           carHeartbeatTask 每 30s POST /car present（夾帶 WROOM 遙測）
 *   core 1：audioPlaybackTask 解 MP3 → I2S 輸出（給 WROOM bt_bridge 轉 A2DP）
 *           loop()：PTT hold-to-talk 錄 INMP441 → POST /audio、方向盤按鍵/遙測 UART、LED
 *           faceTask（FACE_DISPLAY）：頭像即時渲染 24fps，優先權 1，算不完只掉格不搶解碼
 *   斷電＝心跳自然停送，由伺服器 TTL(90s) 收尾停播。
 *
 * 2026-10-31 全時收音改版（PUCK_MIC_WS）：/audio_stream 整條關掉（PUCK_AUDIO_STREAM=0，
 * 函式原地保留、沒被呼叫），讓出的那個 TLS 名額給 /mic_ws 用（常駐 TLS 仍維持最多兩條：
 * 心跳 gHeartbeatFunnelClient + /mic_ws gMicFunnelClient）。靠 /audio_stream ICY 驅動的
 * 臉部嘴型/手勢、封面/底圖換圖、WROOM 曲名這批功能這次先放掉，之後再補。新增
 * micCaptureTask（core 1，讀 I2S 麥→IMA ADPCM→PSRAM ring）+ micWsTask（core 0，常駐
 * WebSocket 上傳到 /mic_ws，收 duck/unduck 控制、轉送 UDP 給手機 DuckListener）。PTT
 * （實體鍵/車機暫停鍵）改成「強制喚醒」：按下送 {"type":"ptt"} 文字訊息，不再自己錄音
 * POST /audio（見 pttEdgeToWake()）。
 *

 * ⚠️ 動手前：cp secrets.h.example secrets.h 並填 WiFi、MARVIN_TOKEN、MARVIN_LOCAL_HOST。（I2S 腳位已實測、不用再查，見下。）
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
#include <WiFiUdp.h>
#include <HTTPClient.h>
#include <driver/i2s.h>
#include <freertos/semphr.h>
#include <esp_heap_caps.h>     // heap_caps_get_free_size/largest_free_block（Funnel TLS 內部 RAM 診斷用）
#include <string.h>
#include <MP3DecoderHelix.h>   // pschatzmann/arduino-libhelix — decode-only(不接管連線),
                                // 塞進既有 audioNetworkTask(填ring buffer)/audioPlaybackTask
                                // (解碼+i2s_write)雙task架構，見 audioPlaybackTask 前的說明
#include "icy_demux.h"         // 車機顯示曲名第一刀：拆 /audio_stream 夾帶的 ICY in-band metadata
#include "icy_fields.h"        // 轉送給 WROOM 前砍掉第 9 欄（bpm）；WROOM 解析器規定剛好 8 欄
#include "cover_fetch.h"   // 換歌下載徽章封面（/car_cover）：JPEG 粗驗、HTTP 標頭解析、遙測字串
#include "ima_adpcm.h"         // /mic_ws 上傳格式：IMA ADPCM 編碼（唯一標準見 ima_adpcm.py）
#include "ws_frame.h"          // /mic_ws 用的最小 WebSocket client frame 組裝/解析 + base64
#include <esp_random.h>        // esp_random()：WS frame mask key、Sec-WebSocket-Key

// 2026-10-24：螢幕還沒到貨，先在 S3 上邊播音樂邊實測 face_rt.h 的即時渲染＋RGB565＋SPI
// 推圖耗時，會不會拖慢 MP3 解碼。SPI 沒接螢幕照樣打時脈，時間/CPU 負擔跟接了一樣。
#ifndef FACE_BENCH
#define FACE_BENCH 0   // 1＝頭像即時渲染壓力測試（不接螢幕也能量），正式版保持 0
#endif

#if FACE_BENCH
#include <SPI.h>
#include <esp_timer.h>
#include <esp_cpu.h>
#define FACE_RT_CYCLES() esp_cpu_get_cycle_count()
#include "face_rt.h"
FaceRtProfile g_face_rt_prof = {};
#endif

// ========== 你要填的 ==========
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
#include "secrets.h"   // WiFi/token/區網IP：複製 secrets.h.example 成 secrets.h 填真值（已 gitignore，別 commit 真值）
#include "device_id.h"   // 身分不在 secrets.h：由晶片 MAC 產生，見下方 setup() 的 format_device_id()
const char* MARVIN_HOST  = "macbook-air.tail7ba8d0.ts.net";   // 不含 https://
const int   MARVIN_PORT  = 443;

static char gDeviceId[16] = "";   // setup() 填：由 ESP.getEfuseMac() 產生，見 device_id.h

// TEMP 實驗（2026-07-25）：/audio_stream 實測 sustained throughput 只有目標 187.5KB/s
// 的 ~55-67%（100-126KB/s），懷疑雙重加密——Tailscale WireGuard 本身已加密，這條又走
// HTTPS/TLS(443)，ESP32 軟體 mbedTLS 解密可能就是瓶頸。puck 目前只在家測（同一台
// Wi-Fi），先試直連 Mac 區網 IP + 明碼 HTTP，看 throughput 是否顯著改善來確認假設。
// 只有這條高頻寬串流走這個路徑，/car 心跳、/now 等低流量請求維持原本 HTTPS 不動。
// ⚠️ 只在家測試網路有效；真的出門用 4G 時這個 IP 打不通，需要退回 Tailscale/Funnel。
const int   MARVIN_LOCAL_PORT = 8790;
// MARVIN_LOCAL_HOST 設 "" ＝只走熱點/Funnel，跳過所有區網嘗試（省下每次持 LWIP_LOCK 白等 0.8–1.2s）
// 用巨集不用函式：Arduino 自動原型會插在第一個函式定義前，放這裡會讓 setLed(LedState) 原型跑到 enum 之前而編譯失敗
#define lanEnabled() (MARVIN_LOCAL_HOST[0] != '\0')

// 2026-10-20：換歌時下載 Mac 預算的徽章封面（/car_cover）進 PSRAM，先做壓力測試（還沒接螢幕）。
// 0＝整段關掉（不配 buffer、不下載、心跳不夾 cover_sum）。
#define COVER_FETCH 1
#define COVER_SIZE_PX 240              // mk1 GC9A01 240×240
#define COVER_BUF_CAP (64 * 1024)      // 240px JPEG 實測約 10–20KB

// 2026-10-24：螢幕已到貨，正式接上 face_rt.h 即時渲染（GC9A01，顯示法照抄
// firmware/face_demo/face_demo.ino）。跟 FACE_BENCH（壓力測試，不接螢幕）共用同一組 SPI
// 腳位，兩者不能同時開。
#ifndef FACE_DISPLAY
#define FACE_DISPLAY 1
#endif

#if FACE_DISPLAY && FACE_BENCH
#error "FACE_DISPLAY 和 FACE_BENCH 共用同一組 SPI 腳位，不能同時開（見各自定義前的註解）"
#endif

#if FACE_DISPLAY
#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include "face_rt.h"
#include "face_motion.h"
#include "speak_sched.h"
#if __has_include("../face_demo/base_sphere.h")
#include "../face_demo/base_sphere.h"
#else
#error "先跑 python3 tools/puck_face/gen_base_sphere.py 產生 firmware/face_demo/base_sphere.h"
#endif

// 腳位/初始化照抄 firmware/face_demo/face_demo.ino（已實機測通：10MHz 初始化、40MHz 畫圖）。
class FaceLGFX : public lgfx::LGFX_Device {
  lgfx::Panel_GC9A01 _panel;
  lgfx::Bus_SPI _bus;
  lgfx::Light_PWM _light;
public:
  FaceLGFX() {
    { auto c = _bus.config();
      c.spi_host = SPI2_HOST; c.spi_mode = 0; c.freq_write = 10000000; c.freq_read = 16000000;
      c.pin_sclk = 21; c.pin_mosi = 47; c.pin_miso = -1; c.pin_dc = 40;
      c.dma_channel = SPI_DMA_CH_AUTO;
      _bus.config(c); _panel.setBus(&_bus); }
    { auto c = _panel.config();
      c.pin_cs = 41; c.pin_rst = 45; c.pin_busy = -1;
      c.panel_width = 240; c.panel_height = 240;
      c.invert = true; c.rgb_order = false; c.readable = false; c.bus_shared = false;
      _panel.config(c); }
    { auto c = _light.config();
      c.pin_bl = 42; c.invert = false; c.freq = 44100; c.pwm_channel = 7;
      _light.config(c); _panel.setLight(&_light); }
    setPanel(&_panel);
  }
  void setFreq(uint32_t f) { _bus.release(); auto c = _bus.config(); c.freq_write = f; _bus.config(c); _bus.init(); }
};
static FaceLGFX g_faceTft;

// faceTask 讀、onIcyMeta()/setLed() 寫的輸入快照，用 portMUX_TYPE 包住的臨界區互斥
// （onIcyMeta 跑在 audioNetworkTask/core 0，setLed 可能從 core 0 或 core 1 呼叫，
// faceTask 釘 core 1——三方都可能同時碰，不能只用 volatile）。
struct FaceInSnapshot {
  face_motion::Override override_;
  bool playing;
  bool haveTrack;
  int bpm;
  uint32_t durMs;
  uint32_t posMs;
  uint32_t posAtMs;        // millis() 時間戳，playing 時用來外插目前 pos
  uint32_t trackKey;
  uint32_t trackChangedAtMs;
  uint32_t lastIcyAtMs;    // 最近一次收到 ICY metadata 的 millis()（串流斷了就當沒歌）
  uint32_t coverSeq;
  bool speaking;           // ICY 第 10 欄依播放位置套用（見 speak_sched.h）
  uint32_t awaitUntilMs;   // postAudio 送出後等回覆的截止時間；0＝未在等
  int pendingGesture;      // ICY 第 11 欄依播放位置套用後還沒播的動作；-1＝無
  int pendingLevel;
  uint32_t pendingAtMs;
  uint32_t skinSeq;        // skinFetch() 交了一張新底圖（或要換回預設頭）就 +1，faceTask 看到變了才換
};
static portMUX_TYPE g_faceInMux = portMUX_INITIALIZER_UNLOCKED;
static FaceInSnapshot g_faceIn = {
  face_motion::OVERRIDE_NONE, false, false, 0, 0, 0, 0, 0, 0, 0, 0, false, 0, -1, 0, 0, 0
};
#endif  // FACE_DISPLAY

// 2026-10-31：全時收音上傳 /mic_ws 上線。PUCK_AUDIO_STREAM=0 整條關掉（函式原地保留，
// 只是沒被呼叫）——跟著關掉的有 ICY 驅動的嘴型/手勢、封面/底圖換圖、WROOM 曲名，這次
// 先放掉，之後再補，不要試圖在這份改動裡順手補。讓出來的那個 TLS 名額給 /mic_ws
// （常駐 TLS 仍是最多兩條：心跳 + /mic_ws）。
#ifndef PUCK_AUDIO_STREAM
#define PUCK_AUDIO_STREAM 0
#endif
// PUCK_MIC_WS=1：常駐收音上傳 /mic_ws + PTT 改成「強制喚醒」（不再自己錄音 POST /audio）。
// =0：維持舊的 PTT hold-to-talk 錄音路徑，行為跟改版前完全相同。
#ifndef PUCK_MIC_WS
#define PUCK_MIC_WS 1
#endif

// ========== 板上按鈕（V1.7；2026-07-17 三顆都實測按過）==========
#define PIN_BTN_PTT    0    // 喚醒/打斷 = 我們的 PTT

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

// ========== 跟 WROOM（bt_bridge）之間的 UART（車機顯示曲名/方向盤按鍵通道）==========
// S3 GPIO17 → WROOM GPIO16、WROOM GPIO17 → S3 GPIO18、GND 共用。
#define PIN_WROOM_UART_TX 17
#define PIN_WROOM_UART_RX 18

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
#if FACE_DISPLAY
  // THINK＝送出後等回應（LED_PLAYING，10s 逾時）；SPEAK 不在這裡決定——由 ICY 第 10 欄
  // 依播放位置套用（見 speak_sched.h + resolve_override），faceTask 組 FaceInputs 時
  // 跟這裡設的 override_ 一起算出最終 override（resolve_override 的優先序見其註解）。
  face_motion::Override ov = face_motion::OVERRIDE_NONE;
  switch (s) {
    case LED_LISTENING: ov = face_motion::OVERRIDE_LISTEN; break;
    case LED_PLAYING:   ov = face_motion::OVERRIDE_THINK; break;
    case LED_ERROR:     ov = face_motion::OVERRIDE_ERROR; break;
    default:            ov = face_motion::OVERRIDE_NONE; break;
  }
  portENTER_CRITICAL(&g_faceInMux);
  g_faceIn.override_ = ov;
  portEXIT_CRITICAL(&g_faceInMux);
#endif
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
      // 送出後等回覆的青燈，10s 上限自動落回待機（回覆從 /audio_stream 播出，沒有明確結束事件）。
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

// 起 INMP441 I2S 麥
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

// 起 PCM5102 I2S 喇叭輸出（TX）
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

// 常駐音訊串流（車載模式）——開機後常駐一條到 /audio_stream 的連線，讀到就播；
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

#if FACE_DISPLAY
static volatile uint32_t g_ringWriteTotal = 0;  // producer 專用：累計寫進 ring 的音訊 bytes
static volatile uint32_t g_ringReadTotal = 0;   // consumer 專用：累計從 ring 讀出的 bytes
static SpeakSched g_speakSched;
static volatile int g_icyPendingSpeak = -1;  // onIcyMeta 寫、network loop 讀
static volatile bool g_icyPendingGestureValid = false;
static volatile uint16_t g_icyPendingGestureSeq = 0;
static volatile int g_icyPendingGesture = -1, g_icyPendingGestureLevel = 0;
static uint16_t g_lastGestureSeq = 0;  // network task 專用
static bool g_gestureSeqBaseline = false;  // network task 專用
#endif  // FACE_DISPLAY

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
#if FACE_DISPLAY
  g_ringWriteTotal += len;
#endif
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
#if FACE_DISPLAY
  g_ringReadTotal += want;
#endif
  return want;
}

// 車機顯示曲名：icy_feed() 湊到一筆完整 metadata 就呼叫這裡轉送給 WROOM（見
// icy_demux.h 的 on_meta 簽名）。只有第一次或換歌（比對前 19 bytes＝"M1␟"+16 hex
// track_id，不含後面每次都變的 pos_ms）才印 Serial log——這裡跑在 audioNetworkTask，
// HWCDC 下 Serial.printf 可能阻塞（見 STREAM_DEBUG_PRINT 註解），能少印就少印。
#if COVER_FETCH
// onIcyMeta（audioNetworkTask）只設 g_coverDirty；真正下載在 carHeartbeatTask 的巡邏迴圈。
static volatile bool g_coverDirty = false;
static uint8_t* g_coverBuf = nullptr;
static size_t g_coverLen = 0;          // 最近一次成功下載的 JPEG 長度（之後給顯示 task 用）
static CoverStats g_coverStats = {0, 0, 0, 0, 0, 0, 0, 0, '-', 0, 0};
static bool g_coverStatsNew = false;
static uint8_t g_coverRetries = 0;
static uint32_t g_coverRetryAt = 0;    // 0＝沒有排重試
#endif

#if FACE_DISPLAY && COVER_FETCH
// 頭像底圖網路換圖（/car_skin）：ICY 第 12 欄帶 Mac 端目前底圖 etag，onIcyMeta 看到變了只設
// g_skinDirty；carHeartbeatTask 巡邏迴圈呼叫 skinFetch() 下載（跟 coverFetch 同一套連線/鎖），
// 交到 g_skinShow 後 skinSeq+1，faceTask 淡出→換底圖→淡入（face_motion::face_skin_step）。
#define SKIN_BYTES (240 * 240 * 3)
static volatile bool g_skinDirty = false;          // onIcyMeta 設、carHeartbeatTask 清
static char g_skinWant[17] = {0};                  // ICY 最新 etag（onIcyMeta 寫；空＝預設頭；讀寫包 g_faceInMux）
static char g_skinHave[17] = {0};                  // 已交給 faceTask 的 etag（carHeartbeatTask 專用）
static bool g_skinHaveValid = false;               // 開機還沒套用過任何 etag
static uint8_t* g_skinBuf = nullptr;               // 下載 buffer（PSRAM，carHeartbeatTask 專用）
static SemaphoreHandle_t gSkinMutex = nullptr;     // 保護 g_skinShow / g_skinShowDefault
static uint8_t* g_skinShow = nullptr;              // 交給 faceTask 的那張（PSRAM）
static bool g_skinShowDefault = false;             // true＝換回 BASE_SPHERE
static uint8_t g_skinRetries = 0;
static uint32_t g_skinRetryAt = 0;
#endif

static uint8_t g_lastMetaPrefix[19];
static size_t g_lastMetaPrefixLen = 0;
static bool g_haveLastMeta = false;
static volatile int g_icyBpm = 0;

static void onIcyMeta(const uint8_t* payload, size_t len, void* /*ctx*/) {
  g_icyBpm = icy_bpm_field(payload, len);
  size_t sendLen = icy_prefix_len(payload, len, 8);
  Serial1.write(payload, sendLen);
  Serial1.write('\n');
  size_t cmpLen = icy_track_cmp_len(payload, len, sizeof(g_lastMetaPrefix));
  bool changed = !g_haveLastMeta || cmpLen != g_lastMetaPrefixLen ||
                 memcmp(payload, g_lastMetaPrefix, cmpLen) != 0;
  if (changed) {
    memcpy(g_lastMetaPrefix, payload, cmpLen);
    g_lastMetaPrefixLen = cmpLen;
    g_haveLastMeta = true;
#if COVER_FETCH
    g_coverDirty = true;   // 只設旗標，交給 carHeartbeatTask 下載（回呼不准直接做網路）
#endif
    Serial.printf("[Meta] %.*s\n", (int)len, payload);
  }
#if FACE_DISPLAY
  // 只設值，不做別的——實際排入 speak_sched 交給 audioNetworkTask 自己的迴圈（那裡才
  // 知道「這批音訊」對應的 g_ringWriteTotal 位置）。
  g_icyPendingSpeak = icy_speak_field(payload, len);
  uint16_t gs; int gg, gl;
  if (icy_gesture_field(payload, len, &gs, &gg, &gl)) {
    g_icyPendingGestureSeq = gs;
    g_icyPendingGesture = gg;
    g_icyPendingGestureLevel = gl;
    g_icyPendingGestureValid = true;
  }
#if COVER_FETCH
  char sk[17];
  if (icy_skin_field(payload, len, sk, sizeof(sk))) {
    portENTER_CRITICAL(&g_faceInMux);
    bool diff = strcmp(sk, g_skinWant) != 0;
    if (diff) strcpy(g_skinWant, sk);
    portEXIT_CRITICAL(&g_faceInMux);
    if (diff) g_skinDirty = true;   // 只設旗標，下載交給 carHeartbeatTask（回呼不准做網路）
  }
#endif
  // 只寫資料，不呼叫任何顯示函式——顯示交給釘死 core 1 的 faceTask 自己的巡邏節奏讀。
  IcyNow icyNow;
  if (icy_now_fields(payload, len, &icyNow)) {
    uint32_t tk = face_motion::fnv1a(icyNow.trackId);
    uint32_t nowMs = millis();
    portENTER_CRITICAL(&g_faceInMux);
    if (!g_faceIn.haveTrack || g_faceIn.trackKey != tk) {
      g_faceIn.trackChangedAtMs = nowMs;
      g_faceIn.trackKey = tk;
      g_faceIn.haveTrack = true;
    }
    g_faceIn.playing = (icyNow.state == 'P');
    g_faceIn.bpm = g_icyBpm;
    g_faceIn.durMs = icyNow.durMs;
    g_faceIn.posMs = icyNow.posMs;
    g_faceIn.posAtMs = nowMs;
    g_faceIn.lastIcyAtMs = nowMs;
    portEXIT_CRITICAL(&g_faceInMux);
  }
#endif
}

#if FACE_DISPLAY
// faceTask（釘 core 1）讀、carHeartbeat()（釘 core 0）每 60s 列印後清零——都是診斷用
// volatile 計數，偶爾少算一筆可接受，不加鎖。
static TaskHandle_t faceTaskHandle = nullptr;
static volatile uint32_t g_faceFrames = 0, g_faceLate = 0, g_faceGestures = 0;
static volatile uint64_t g_faceRenderUs = 0, g_facePushUs = 0;

// PSRAM 配置；setup() 失敗時維持 nullptr，不建 faceTask。
static uint8_t* g_faceBase = nullptr;
static uint8_t* g_faceOut = nullptr;
static FaceRtWork* g_faceWork = nullptr;

// coverFetch()（carHeartbeatTask，core 0）寫、faceTask（core 1）讀，用 gCoverMutex 互斥。
// g_coverBuf 本身（COVER_FETCH 既有下載 buffer）只給 coverFetch() 自己用，不跨 task 共用。
static SemaphoreHandle_t gCoverMutex = nullptr;
static uint8_t* g_coverShow = nullptr;
static size_t g_coverShowLen = 0;
#endif  // FACE_DISPLAY

void audioNetworkTask(void* pv) {
  Serial.println("[Stream] audioNetworkTask 已啟動");
  // 2026-07-25：512→4096。實測 idle 靜音期間 ring buffer 仍穩定下探（96KB→3.8KB/~2s，
  // 實際補貨速率只有需求的 ~75%），優先權已經調過還是一樣，指向每次 readBytes 的 TLS
  // 解密呼叫開銷才是瓶頸，不是排程。加大單次讀取量減少呼叫次數換 throughput。
  uint8_t buf[4096];
  static uint8_t audioBuf[4096];   // icy_feed() 拆出的音訊 bytes 落在這裡，再交給 ringWrite
  IcyDemux demux;
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
    // 靜音、心跳log也停。設短逾時，見 funnelEnsureConnected() 前的註解。
    funnelClient.setHandshakeTimeout(5);
    bool connectOk = false;
    if (lanEnabled()) {
      LWIP_LOCK();
      connectOk = localClient.connect(MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 1200);
      LWIP_UNLOCK();
    }
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
                 "Host: " + host + "\r\nIcy-MetaData: 1\r\nConnection: close\r\n\r\n";
    LWIP_LOCK(); client.print(req); LWIP_UNLOCK();

    String status;
    lockedReadLine(client, status, 20000);
    bool ok200 = status.indexOf("200") > 0;
    bool chunked = false;
    uint32_t metaint = 0;
    while (client.connected()) {
      String h;
      if (!lockedReadLine(client, h, 20000)) break;   // 逾時/斷線＝跳出，走下面的重連
      if (h.length() == 0) break;                     // 空行＝標頭結束
      if (h.indexOf("chunked") >= 0) chunked = true;
      String hl = h; hl.toLowerCase();
      int metaIdx = hl.indexOf("icy-metaint:");
      if (metaIdx >= 0) {
        metaint = (uint32_t)strtoul(hl.c_str() + metaIdx + strlen("icy-metaint:"), nullptr, 10);
      }
    }

    if (!ok200 || !chunked) {
      Serial.printf("[Stream] /audio_stream 非預期回應（200=%d chunked=%d）：%s",
                    ok200, chunked, status.c_str());
      LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
      vTaskDelay(pdMS_TO_TICKS(5000));        // 車載模式未接串流輸出等更久，避免熱迴圈
      continue;
    }
    Serial.println("[Stream] /audio_stream 連上，開始灌 buffer");
    icy_init(demux, metaint);   // 沒看到 icy-metaint header 時 metaint=0，全部當音訊
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
#if FACE_DISPLAY
    speak_sched_reset_producer(g_speakSched);
    g_icyPendingSpeak = -1;
    g_gestureSeqBaseline = false;
    g_icyPendingGestureValid = false;
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
        size_t an = icy_feed(demux, buf, n, audioBuf, onIcyMeta, nullptr);
        if (an) ringWrite(audioBuf, an);   // 只塞 buffer，不碰 I2S——播放節奏交給 audioPlaybackTask
#if FACE_DISPLAY
        // pos 用寫完這批後的總量，最多比真實位置晚一批（≤4096 bytes ≈0.25s），可接受。
        {
          int sp = g_icyPendingSpeak;
          if (sp >= 0) {
            g_icyPendingSpeak = -1;
            speak_sched_push(g_speakSched, g_ringWriteTotal, sp == 1);
          }
        }
        if (g_icyPendingGestureValid) {
          g_icyPendingGestureValid = false;
          uint16_t gs = g_icyPendingGestureSeq;
          if (!g_gestureSeqBaseline) { g_lastGestureSeq = gs; g_gestureSeqBaseline = true; }   // 連線後第一筆只當基準，不重播舊動作
          else if (gs != g_lastGestureSeq) { g_lastGestureSeq = gs; speak_sched_push_gesture(g_speakSched, g_ringWriteTotal, g_icyPendingGesture, g_icyPendingGestureLevel); }
        }
#endif
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

#if FACE_BENCH
// FACE_BENCH 量測用全域變數：解碼/I2S/ring 健康狀態統計，窗口結束由 faceBenchTask 讀取
// 後歸零（g_fbRingMin 重設為 0xFFFFFFFF）。跨 task 讀寫不加鎖——量測用，偶爾少算一筆可接受。
static volatile uint32_t g_fbDecUs = 0;      // 本窗口 MP3 解碼淨 CPU 時間（扣掉 i2s_write 阻塞）
static volatile uint32_t g_fbDecMaxUs = 0;   // 本窗口單次 mp3Decoder.write 淨耗時最大值
static volatile uint32_t g_fbI2sUs = 0;      // mp3DataCallback 內 i2s_write 累積阻塞時間（給上面扣除用）
static volatile uint32_t g_fbRingEmpty = 0;  // 本窗口播放中 ring 見底次數（avail==0 且 streamPrimed）
static volatile uint32_t g_fbRingMin = 0xFFFFFFFF;  // 本窗口播放中 ringUsed() 最低值
static uint32_t g_fbConvUs = 0;  // 本窗口 fbPush 內 RGB565 轉換累積耗時
static uint32_t g_fbSpiUs = 0;   // 本窗口 fbPush 內 fbSpi.writeBytes 傳輸累積耗時
#endif

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
  size_t written;
#if FACE_BENCH
  int64_t _fbT0 = esp_timer_get_time();
#endif
  i2s_write(I2S_SPK_PORT, pcm_buffer, len * sizeof(short), &written, portMAX_DELAY);
#if FACE_BENCH
  g_fbI2sUs += (uint32_t)(esp_timer_get_time() - _fbT0);
#endif
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
#if FACE_DISPLAY
      // 讀取位置跳到寫入端，未套用的事件全部立刻消化＝落到 producer 最新排入的狀態。
      // 不能清空佇列：network task 可能已經排了新連線的第一筆，producer 記得「已排過」
      // 就不會再排同值，清掉會讓整段講話期間嘴都不動。
      g_ringReadTotal = g_ringWriteTotal;
      SpeakApplyOut out;
      if (speak_sched_apply(g_speakSched, g_ringReadTotal, &out)) {
        portENTER_CRITICAL(&g_faceInMux);
        if (out.speakApplied) g_faceIn.speaking = out.speaking;
        if (out.gesture >= 0) {
          g_faceIn.pendingGesture = out.gesture;
          g_faceIn.pendingLevel = out.level;
          g_faceIn.pendingAtMs = millis();
        }
        portEXIT_CRITICAL(&g_faceInMux);
      }
#endif
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
#if FACE_BENCH
    if (avail < g_fbRingMin) g_fbRingMin = avail;
#endif
    if (avail == 0) {
#if FACE_BENCH
      g_fbRingEmpty++;
#endif
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
#if FACE_DISPLAY
    {
      SpeakApplyOut out;
      if (speak_sched_apply(g_speakSched, g_ringReadTotal, &out)) {
        portENTER_CRITICAL(&g_faceInMux);
        if (out.speakApplied) g_faceIn.speaking = out.speaking;
        if (out.gesture >= 0) {
          g_faceIn.pendingGesture = out.gesture;
          g_faceIn.pendingLevel = out.level;
          g_faceIn.pendingAtMs = millis();
        }
        portEXIT_CRITICAL(&g_faceInMux);
      }
    }
#endif
#if FACE_BENCH
    int64_t _fbT0 = esp_timer_get_time();
    uint32_t _fbI2s0 = g_fbI2sUs;
#endif
    mp3Decoder.write(buf, n);   // 解碼出的 PCM 經 mp3DataCallback 同步 i2s_write
#if FACE_BENCH
    {
      uint32_t net = (uint32_t)(esp_timer_get_time() - _fbT0) - (g_fbI2sUs - _fbI2s0);
      g_fbDecUs += net;
      if (net > g_fbDecMaxUs) g_fbDecMaxUs = net;
    }
#endif
  }
}

// 車載 present 心跳（POST /car）——伺服器 TTL 預設 90s，這裡每 30s 送一次留足
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

  // path 已含 query（如 /audio?device=…）時 token 接在後面用 &t=，否則用 ?t=
  String header = String("POST ") + path + (strchr(path, '?') ? "&t=" : "?t=") + MARVIN_TOKEN + " HTTP/1.1\r\n" +
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

// 2026-08-13：postHttp() 的 keep-alive 版——不用在每次心跳都重新 TLS handshake（ECDHE
// 是 ESP32 最貴的 CPU 操作，同 core 上會跟 audioNetworkTask 搶 LWIP_LOCK/CPU，實機在
// 熱點/Funnel 下觀察到心跳連續 HTTP -1、/audio_stream 斷續，見 carHeartbeat() 前的
// 說明）。跟 postHttp() 不同：呼叫端負責保留 client 物件跨呼叫存活、不主動 stop()；
// 這裡必須精確讀完 Content-Length 那麼多 body bytes 才能把 socket 讀取位置停在下一個
// response 的開頭——不能像 postHttp() 那樣「只看 status line、不管 body」，keep-alive
// 連線上殘留沒讀完的 body 會讓下一次心跳從殘留資料開始解析、狀態碼全亂。回傳
// false＝連線已經斷了或讀取異常，呼叫端要自己 stop() 該連線、下次重新 connect()+
// handshake；true＝連線仍活著，可以留給下一次心跳直接複用。
static bool postHttpKeepAlive(WiFiClient& client, const char* host, const char* path,
                               const char* contentType, const uint8_t* body, size_t bodyLen,
                               uint32_t readTimeoutMs, int* outCode) {
  *outCode = -1;
  if (!client.connected()) return false;

  // path 已含 query（如 /car?…）時 token 接在後面用 &t=，否則用 ?t=
  String header = String("POST ") + path + (strchr(path, '?') ? "&t=" : "?t=") + MARVIN_TOKEN + " HTTP/1.1\r\n" +
                  "Host: " + host + "\r\n" +
                  "Content-Type: " + contentType + "\r\n" +
                  "Content-Length: " + bodyLen + "\r\n" +
                  "Connection: keep-alive\r\n\r\n";
  LWIP_LOCK();
  client.print(header);
  client.write(body, bodyLen);
  LWIP_UNLOCK();

  String status;
  if (!lockedReadLine(client, status, readTimeoutMs)) return false;
  int code = 0;
  int sp1 = status.indexOf(' ');
  if (sp1 > 0) code = status.substring(sp1 + 1, sp1 + 4).toInt();

  long contentLength = -1;
  for (;;) {
    String h;
    if (!lockedReadLine(client, h, readTimeoutMs)) return false;
    if (h.length() == 0) break;   // 空行＝標頭結束
    if (h.startsWith("Content-Length:") || h.startsWith("content-length:")) {
      contentLength = h.substring(h.indexOf(':') + 1).toInt();
    }
  }
  if (contentLength > 0) {
    uint8_t drain[256];
    long remain = contentLength;
    while (remain > 0) {
      size_t want = remain < (long)sizeof(drain) ? (size_t)remain : sizeof(drain);
      size_t n = lockedReadBytes(client, drain, want, readTimeoutMs);
      if (n == 0) return false;   // 讀不到＝斷線，逼下一次重新 connect()
      remain -= (long)n;
    }
  }
  *outCode = code;
  return true;
}

// GET 進呼叫端給的 buffer（沿用 lockedReadLine/lockedReadBytes 的鎖紀律）。呼叫端已 connect。
// 回傳 true＝回應完整讀完（keep-alive 連線位置乾淨可複用）；false＝讀取異常，呼叫端要 stop()。
// 200 且 Content-Length <= cap 才寫進 buf（*outLen）；其餘狀態或太大一律讀掉丟棄。
static bool getHttpToBuf(WiFiClient& client, const char* host, const char* path, bool keepAlive,
                         uint8_t* buf, size_t cap, size_t* outLen, int* outCode, uint32_t readTimeoutMs) {
  *outLen = 0;
  *outCode = -1;
  if (!client.connected()) return false;

  String header = String("GET ") + path + (strchr(path, '?') ? "&t=" : "?t=") + MARVIN_TOKEN + " HTTP/1.1\r\n" +
                  "Host: " + host + "\r\n" +
                  "Connection: " + (keepAlive ? "keep-alive" : "close") + "\r\n\r\n";
  LWIP_LOCK();
  client.print(header);
  LWIP_UNLOCK();

  String status;
  if (!lockedReadLine(client, status, readTimeoutMs)) return false;
  int code = http_status_code(status.c_str());

  long contentLength = -1;
  for (;;) {
    String h;
    if (!lockedReadLine(client, h, readTimeoutMs)) return false;
    if (h.length() == 0) break;   // 空行＝標頭結束
    long cl = http_content_length(h.c_str());
    if (cl >= 0) contentLength = cl;
  }
  if (contentLength < 0) return false;

  if (code == 200 && (size_t)contentLength <= cap) {
    size_t n = lockedReadBytes(client, buf, (size_t)contentLength, readTimeoutMs);
    if (n != (size_t)contentLength) return false;
    *outLen = n;
  } else {
    uint8_t drain[256];
    long remain = contentLength;
    while (remain > 0) {
      size_t want = remain < (long)sizeof(drain) ? (size_t)remain : sizeof(drain);
      size_t n = lockedReadBytes(client, drain, want, readTimeoutMs);
      if (n == 0) return false;
      remain -= (long)n;
    }
  }
  *outCode = code;
  return true;
}

// 2026-10-14：WROOM 的 DIAG/統計摘要只存在它自己的 NVS，要拔線插 Mac 才讀得到，而且一
// 插上序列埠就重開一次機、那趟車上 session 的紀錄就沒了。WROOM 已經有一條 UART 把方向盤
// 按鍵轉送過來（pollWheelFromWroom()），這裡原樣加碼：WROOM 送 "D|"/"S|" 開頭的行時存進
// 這兩個暫存，carHeartbeatTask 每 30s 心跳時順便夾帶出去——不開新連線（第三條 TLS 握手
// 失敗的前車之鑑，見 gFunnelMutex 前的說明），一律搭心跳。loop task（pollWheelFromWroom）
// 寫、carHeartbeatTask 讀，兩個不同 task 用 gWroomLogMutex 互斥。
static SemaphoreHandle_t gWroomLogMutex = nullptr;
static char gWroomSum[160];
static bool gWroomSumNew = false;
static char gWroomDiag[8][64];
static uint8_t gWroomDiagN = 0;

// 2026-08-13：心跳走 Funnel 時複用同一個 TLS 連線（keep-alive），不是 postHttp() 那種
// 用完即丟的 client——避免每 30 秒都重新做一次完整 handshake。
static WiFiClientSecure gHeartbeatFunnelClient;

// 2026-10-14：這塊板子的 Arduino 預編 CONFIG_MBEDTLS_INTERNAL_MEM_ALLOC=y（TLS 只能用
// 內部 RAM，不能用 PSRAM）。熱點（無區網）下實測：心跳走 gHeartbeatFunnelClient 這條
// 常駐 TLS 一直成功，但 postAudio() 原本自己 new 一個 WiFiClientSecure 開「第三條」
// TLS（/audio_stream 常駐一條、心跳一條）——內部 RAM 不夠，第三條握手失敗，HTTP -1。
// 解法：postAudio() 的 Funnel 路徑也直接複用 gHeartbeatFunnelClient，跟心跳共用同一條
// 連線；用 gFunnelMutex 互斥，避免兩邊同時讀寫同一個 client 物件、或心跳在上傳音訊時
// 硬插進來把對話回應的 response 吃掉。
static SemaphoreHandle_t gFunnelMutex = nullptr;

// 把 carHeartbeat() 原本「connected() 檢查→沒連就 setInsecure/setHandshakeTimeout/
// LWIP_LOCK connect/UNLOCK→成功就 setTimeout(500)」那段抽出來，postAudio() 也要用。
// 呼叫端必須已持有 gFunnelMutex。
// ⚠️ WiFiClientSecure 預設 handshake_timeout=120000ms，跟 connect() 的 timeout 參數是兩回事
// （那個只管 TCP 連線階段）——握手卡住最長會空等 2 分鐘，所以這裡先設短。
static bool funnelEnsureConnected(uint32_t handshakeTimeoutS, int32_t connectTimeoutMs) {
  bool alive = gHeartbeatFunnelClient.connected();
  if (!alive) {
    gHeartbeatFunnelClient.setInsecure();
    gHeartbeatFunnelClient.setHandshakeTimeout(handshakeTimeoutS);   // 見 funnelEnsureConnected() 前的註解：預設120s跟connect()逾時無關
    LWIP_LOCK();
    alive = gHeartbeatFunnelClient.connect(MARVIN_HOST, MARVIN_PORT, connectTimeoutMs);
    LWIP_UNLOCK();
    if (alive) gHeartbeatFunnelClient.setTimeout(500);
  }
  return alive;
}

// WROOM 轉送過來的 DIAG/摘要文字可能含引號/反斜線/控制字元，要跳脫才能塞進 JSON 字串
// 值。保證 dst '\0' 結尾、不溢位，回傳實際寫入長度（不含結尾 '\0'）。
static size_t json_escape(char* dst, size_t cap, const char* src) {
  size_t o = 0;
  for (const char* s = src; *s && o + 1 < cap; s++) {
    unsigned char c = (unsigned char)*s;
    if (c == '"' || c == '\\') {
      if (o + 2 >= cap) break;
      dst[o++] = '\\';
      dst[o++] = (char)c;
    } else if (c < 0x20) {
      dst[o++] = ' ';
    } else {
      dst[o++] = (char)c;
    }
  }
  dst[o] = '\0';
  return o;
}

// 2026-08-13：熱點/Funnel 下每次心跳都無條件先試區網——LWIP_LOCK 包住整個 connect()
// timeout，區網打不到時等於每 30 秒白白持鎖最多 timeout 那麼久，直接跟 audioNetworkTask
// 搶讀取視窗，是斷續的另一個貢獻源（跟上面的 Funnel keep-alive 是同一個問題的兩面：
// 「連線建立太貴」+「明知會失敗還硬試」）。記住上次心跳走哪條路：確定不在區網範圍時，
// 接下來幾輪跳過區網嘗試直接走 Funnel；每 gHeartbeatSkipLanRounds 輪（約5分鐘）重新
// 探一次區網，偵測「開回家了」。
static bool gHeartbeatTryLan = true;
static uint8_t gHeartbeatSkipLanRounds = 0;

#if FACE_DISPLAY
// 只呼叫一次：WiFi 第一次連上後設系統時間（carHeartbeat() 只在 WL_CONNECTED 時被呼叫，
// 不管是開機首次連上還是斷線重連，天然涵蓋「WiFi 第一次連上後」這個時機）。
static bool g_ntpStarted = false;
static void faceEnsureNtp() {
  if (g_ntpStarted) return;
  g_ntpStarted = true;
  LWIP_LOCK();
  configTzTime("CST-8", "time.google.com", "pool.ntp.org");
  LWIP_UNLOCK();
}

// 每 60s（心跳週期的兩倍）印一行頭像渲染健康度，印完歸零。
static uint32_t g_faceStatsLastMs = 0;
static void faceLogStats() {
  uint32_t now = millis();
  if (g_faceStatsLastMs != 0 && now - g_faceStatsLastMs < 60000) return;
  uint32_t elapsedMs = now - g_faceStatsLastMs;   // 斷線時心跳會拖過 60s，用實際間隔算 fps
  g_faceStatsLastMs = now;
  uint32_t frames = g_faceFrames, late = g_faceLate, gestures = g_faceGestures;
  uint64_t renderUs = g_faceRenderUs, pushUs = g_facePushUs;
  g_faceFrames = 0; g_faceLate = 0; g_faceRenderUs = 0; g_facePushUs = 0; g_faceGestures = 0;
  double fps = (frames && elapsedMs) ? frames * 1000.0 / elapsedMs : 0.0;
  double renderMs = frames ? (renderUs / 1000.0) / frames : 0.0;
  double pushMs = frames ? (pushUs / 1000.0) / frames : 0.0;
  portENTER_CRITICAL(&g_faceInMux);
  bool spk = g_faceIn.speaking;
  portEXIT_CRITICAL(&g_faceInMux);
  // pfree/pmin：PSRAM 剩餘與開機以來最低水位（v2 硬體評估 2MB PSRAM 夠不夠用）
  Serial.printf("[Face] fps=%.1f late=%u render=%.2fms push=%.2fms stack_hw=%u ifree=%u imin=%u pfree=%u pmin=%u spk=%d gst=%u\n",
                fps, (unsigned)late, renderMs, pushMs,
                faceTaskHandle ? (unsigned)uxTaskGetStackHighWaterMark(faceTaskHandle) : 0,
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_SPIRAM),
                (int)spk, (unsigned)gestures);
}
#endif

void carHeartbeat() {
#if FACE_DISPLAY
  faceEnsureNtp();
  faceLogStats();
#endif
  // 先試區網明碼（快、家用WiFi成立）；連不到（出門）就退回 Funnel TLS。
  //
  // 2026-08-12：實機用 serial 監控抓到 WiFi 明明還連著（deckB 網路 task 持續在收音訊）
  // 但心跳區網+Funnel兩條路連續 HTTP -1——connect() 前要先搶 LWIP_LOCK，這個 task
  // 優先權=1，跟同核心的 audioNet/deckNet（優先權=2）搶不過，逾時前常常連鎖都還沒搶到
  // 就過期。逾時值加大給排程延遲留餘裕（見下面 carHeartbeatTask 建立時優先權也一併調高）。
  // body 加大到 1024：除了原本的 state/device，還要夾帶 WROOM 遙測（wroom_sum 一筆
  // ~140 字 + wroom_diag 最多 8 筆 ~64 字），搭這班心跳一起送出，不開新連線（見
  // gWroomLogMutex 前的說明）。
  static char body[1024];
  size_t len = (size_t)snprintf(body, sizeof(body), "{\"state\":\"present\",\"device\":\"%s\"",
                                 gDeviceId);
  if (xSemaphoreTake(gWroomLogMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
    if (gWroomSumNew) {
      char esc[320];
      json_escape(esc, sizeof(esc), gWroomSum);
      size_t escLen = strlen(esc);
      // +2 給尾端收尾的 "}" 留餘裕，放不下就跳過這次、下次心跳再試
      if (len + strlen(",\"wroom_sum\":\"\"") + escLen + 2 < sizeof(body)) {
        int n = snprintf(body + len, sizeof(body) - len, ",\"wroom_sum\":\"%s\"", esc);
        if (n > 0) {
          len += (size_t)n;
          gWroomSumNew = false;
        }
      }
    }
    if (gWroomDiagN > 0) {
      size_t savedLen = len;
      int n = snprintf(body + len, sizeof(body) - len, ",\"wroom_diag\":[");
      if (n > 0 && len + (size_t)n < sizeof(body)) {
        len += (size_t)n;
        uint8_t fitCount = 0;
        for (uint8_t i = 0; i < gWroomDiagN; i++) {
          char esc[130];
          json_escape(esc, sizeof(esc), gWroomDiag[i]);
          size_t escLen = strlen(esc);
          // (逗號) + 兩個引號 + 內容 + 尾端 "]}" 留餘裕
          size_t needed = (fitCount > 0 ? 1 : 0) + 2 + escLen + 2;
          if (len + needed >= sizeof(body)) break;   // 放不下，剩下的留到下次心跳
          if (fitCount > 0) body[len++] = ',';
          body[len++] = '"';
          memcpy(body + len, esc, escLen);
          len += escLen;
          body[len++] = '"';
          fitCount++;
        }
        if (fitCount > 0) {
          body[len++] = ']';
          // 只清掉成功放進 body 的前 fitCount 筆，其餘往前移，留到下次心跳
          for (uint8_t i = fitCount; i < gWroomDiagN; i++) {
            memcpy(gWroomDiag[i - fitCount], gWroomDiag[i], sizeof(gWroomDiag[0]));
          }
          gWroomDiagN -= fitCount;
        } else {
          len = savedLen;   // 一筆都放不下，整段 header 退回，別留下空陣列
        }
      } else {
        len = savedLen;
      }
    }
    xSemaphoreGive(gWroomLogMutex);
  }
#if COVER_FETCH
  if (g_coverStatsNew) {
    char cs[160];
    cover_sum_format(cs, sizeof(cs), g_coverStats);   // 只有數字/英文/=/空白，不用跳脫
    if (len + strlen(",\"cover_sum\":\"\"") + strlen(cs) + 2 < sizeof(body)) {
      int n = snprintf(body + len, sizeof(body) - len, ",\"cover_sum\":\"%s\"", cs);
      if (n > 0) { len += (size_t)n; g_coverStatsNew = false; }
    }
  }
#endif
  if (len + 1 >= sizeof(body)) len = sizeof(body) - 2;   // 理論上不會發生，前面都留了餘裕，保底別溢位
  body[len++] = '}';
  body[len] = '\0';
  int code = -1;
  bool lanAttempted = gHeartbeatTryLan && lanEnabled();
  if (lanAttempted) {
    // timeout 從 3000ms 砍到 800ms：真的在區網範圍時連線幾毫秒內就會成功，不需要那麼
    // 長的容忍度；失敗時（不在區網範圍）的最壞持鎖時間跟著砍掉，別讓明知可能失敗的
    // 嘗試把 LWIP_LOCK 一鎖鎖 3 秒。
    WiFiClient localClient;
    code = postHttp(localClient, MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 800,
                     "/car", "application/json", (const uint8_t*)body, strlen(body), 4000);
  }

  if (code > 0) {
    gHeartbeatTryLan = true;
    gHeartbeatSkipLanRounds = 0;
    Serial.printf("[Car] present 心跳(區網) HTTP %d\n", code);
  } else {
    if (lanAttempted) {
      // 這輪剛確認區網不通：接下來跳過幾輪的區網嘗試，省下白白持鎖的時間；輪數歸零
      // 後重新探一次（偵測「開回家了」）。
      gHeartbeatTryLan = false;
      gHeartbeatSkipLanRounds = 9;   // 9 輪 × 30s ≈ 4.5 分鐘後重新探一次區網
    } else if (gHeartbeatSkipLanRounds > 0 && --gHeartbeatSkipLanRounds == 0) {
      gHeartbeatTryLan = true;
    }

    // Funnel：跟 postAudio() 共用 gHeartbeatFunnelClient，用 gFunnelMutex 互斥（見
    // gFunnelMutex 前的說明）——keep-alive 複用連線，連線還活著就直接送、不重新握手；
    // 斷了（或這是第一次）才真的 connect()+handshake。
    if (xSemaphoreTake(gFunnelMutex, pdMS_TO_TICKS(20000)) == pdTRUE) {
      bool alive = funnelEnsureConnected(8, 8000);
      if (alive) {
        int httpCode = -1;
        bool ok = postHttpKeepAlive(gHeartbeatFunnelClient, MARVIN_HOST, "/car", "application/json",
                                     (const uint8_t*)body, strlen(body), 6000, &httpCode);
        if (!ok) { LWIP_LOCK(); gHeartbeatFunnelClient.stop(); LWIP_UNLOCK(); }
        code = ok ? httpCode : -1;
      } else {
        code = -1;
      }
      xSemaphoreGive(gFunnelMutex);
    } else {
      code = -1;
      Serial.println("[Car] Funnel 連線忙（上傳中），跳過這輪");
    }
    Serial.printf("[Car] present 心跳(Funnel) HTTP %d\n", code);
  }
  if (code <= 0) { Serial.println("[Car] ⚠️ 心跳區網+Funnel都失敗，跳過這輪"); return; }
  // 2026-08-15：開機時 testFunnelNow() 若剛好撞上瞬斷會設紅燈，之後除了「WiFi 整個斷線
  // 又重連」外沒有任何路徑會清掉——即使 WiFi 全程連著、心跳其實每 30s 都在成功，紅燈
  // 還是會卡一整趟車程（見碰紅燈前讀的記憶）。這裡心跳只要拿到回應就證明連線是通的，
  // 順手清掉舊的錯誤燈號；只在真的是 LED_ERROR 時才動，別去打斷 LISTENING/PLAYING
  // 這種正在進行中的互動狀態。
  if (ledState == LED_ERROR) setLed(LED_CONNECTED);
  // 診斷用堆疊水位（見上方 2026-07-25 註解）：已排除堆疊溢位假說，繼續留著當健康度
  // 觀察——四顆任務裡任何一個持續下探都值得回頭查。
  Serial.printf("[StackWM] loopTask=%u carHeartbeat=%u audioNet=%u audioPlay=%u words\n",
                loopTaskHandle ? (unsigned)uxTaskGetStackHighWaterMark(loopTaskHandle) : 0,
                (unsigned)uxTaskGetStackHighWaterMark(NULL),
                audioNetTaskHandle ? (unsigned)uxTaskGetStackHighWaterMark(audioNetTaskHandle) : 0,
                audioPlayTaskHandle ? (unsigned)uxTaskGetStackHighWaterMark(audioPlayTaskHandle) : 0);
}

#if COVER_FETCH
// 換歌下載封面。只在 carHeartbeatTask 裡呼叫（跟心跳同 task，stats 不用另外加鎖）。
// 區網優先（跟心跳同一個 gHeartbeatTryLan 判斷）；Funnel 一律複用 gHeartbeatFunnelClient＋
// gFunnelMutex——不開第三條 TLS（見 gFunnelMutex 前的說明）。fromRetry=false＝新換歌。
static void coverFetch(bool fromRetry) {
  g_coverDirty = false;
  if (!fromRetry) {
    g_coverRetries = 0;
    g_coverRetryAt = 0;
  }
  if (!g_coverBuf || WiFi.status() != WL_CONNECTED) return;

  char path[64];
  snprintf(path, sizeof(path), "/car_cover?which=current&size=%d", COVER_SIZE_PX);
  uint32_t t0 = millis();
  uint32_t waitMs = 0;
  int code = -1;
  size_t n = 0;
  char via = '-';
  bool ok = false;

  if (gHeartbeatTryLan && lanEnabled()) {
    WiFiClient c;
    LWIP_LOCK();
    bool con = c.connect(MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 800);
    LWIP_UNLOCK();
    if (con) {
      c.setTimeout(500);
      via = 'L';
      ok = getHttpToBuf(c, MARVIN_LOCAL_HOST, path, false, g_coverBuf, COVER_BUF_CAP, &n, &code, 4000);
      LWIP_LOCK();
      c.stop();
      LWIP_UNLOCK();
    }
  }

  if (code <= 0) {
    uint32_t tw = millis();
    if (xSemaphoreTake(gFunnelMutex, pdMS_TO_TICKS(2000)) == pdTRUE) {
      waitMs = millis() - tw;
      via = 'F';
      if (funnelEnsureConnected(8, 8000)) {
        ok = getHttpToBuf(gHeartbeatFunnelClient, MARVIN_HOST, path, true, g_coverBuf, COVER_BUF_CAP, &n, &code, 6000);
        if (!ok) { LWIP_LOCK(); gHeartbeatFunnelClient.stop(); LWIP_UNLOCK(); }
      }
      xSemaphoreGive(gFunnelMutex);
    } else {
      g_coverStats.busy++;
      g_coverStatsNew = true;
      if (g_coverRetries < 2) {
        g_coverRetries++;
        g_coverRetryAt = millis() + 5000;
        if (g_coverRetryAt == 0) g_coverRetryAt = 1;
      } else {
        g_coverRetries = 0;
        g_coverRetryAt = 0;
      }
      Serial.println("[Cover] Funnel 鎖忙，稍後重試");
      return;
    }
  }

  uint32_t ms = millis() - t0;
  if (ok && code == 200 && cover_jpeg_valid(g_coverBuf, n)) {
    g_coverStats.ok++;
    g_coverLen = n;
    g_coverStats.lastBytes = n;
    g_coverRetries = 0;
#if FACE_DISPLAY
    if (g_coverShow) {
      if (xSemaphoreTake(gCoverMutex, pdMS_TO_TICKS(500)) == pdTRUE) {
        memcpy(g_coverShow, g_coverBuf, n);
        g_coverShowLen = n;
        portENTER_CRITICAL(&g_faceInMux);
        g_faceIn.coverSeq++;
        portEXIT_CRITICAL(&g_faceInMux);
        xSemaphoreGive(gCoverMutex);
      } else {
        Serial.println("[Cover] gCoverMutex 忙，這張跳過（faceTask 正在畫）");
      }
    }
#endif
  } else if (ok && code == 404) {
    g_coverStats.none++;
    g_coverRetries = 0;
  } else {
    g_coverStats.fail++;
    if (g_coverRetries < 2) {
      g_coverRetries++;
      g_coverRetryAt = millis() + 5000;
      if (g_coverRetryAt == 0) g_coverRetryAt = 1;
    } else {
      g_coverRetries = 0;
      g_coverRetryAt = 0;
    }
  }
  g_coverStats.lastMs = ms;
  g_coverStats.lastWaitMs = waitMs;
  g_coverStats.lastVia = via;
  if (ms > g_coverStats.maxMs) g_coverStats.maxMs = ms;
  g_coverStats.ifree = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  g_coverStats.imin = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
  g_coverStatsNew = true;
  Serial.printf("[Cover] HTTP %d ok=%d %uB %ums via=%c wait=%ums ifree=%u imin=%u\n",
                code, (int)ok, (unsigned)n, (unsigned)ms, via, (unsigned)waitMs,
                (unsigned)g_coverStats.ifree, (unsigned)g_coverStats.imin);
}
#endif

#if FACE_DISPLAY && COVER_FETCH
static void skinScheduleRetry() {
  if (g_skinRetries < 2) {
    g_skinRetries++;
    g_skinRetryAt = millis() + 5000;
    if (g_skinRetryAt == 0) g_skinRetryAt = 1;
  } else {
    g_skinRetries = 0;
    g_skinRetryAt = 0;
  }
}

// 交給 faceTask：data=nullptr＝換回預設頭。拿不到 gSkinMutex 回 false（呼叫端排重試）。
static bool skinHandOff(const uint8_t* data) {
  if (xSemaphoreTake(gSkinMutex, pdMS_TO_TICKS(500)) != pdTRUE) return false;
  if (data) memcpy(g_skinShow, data, SKIN_BYTES);
  g_skinShowDefault = (data == nullptr);
  portENTER_CRITICAL(&g_faceInMux);
  g_faceIn.skinSeq++;
  portEXIT_CRITICAL(&g_faceInMux);
  xSemaphoreGive(gSkinMutex);
  return true;
}

// 只在 carHeartbeatTask 裡呼叫。結構照 coverFetch：區網優先、Funnel 複用 gHeartbeatFunnelClient＋gFunnelMutex。
static void skinFetch(bool fromRetry) {
  g_skinDirty = false;
  if (!fromRetry) {
    g_skinRetries = 0;
    g_skinRetryAt = 0;
  }
  if (!g_skinBuf || !g_skinShow || WiFi.status() != WL_CONNECTED) return;
  char want[17];
  portENTER_CRITICAL(&g_faceInMux);
  strcpy(want, g_skinWant);
  portEXIT_CRITICAL(&g_faceInMux);
  if (g_skinHaveValid && strcmp(want, g_skinHave) == 0) return;

  uint32_t t0 = millis();
  int code = -1;
  size_t n = 0;
  char via = '-';
  bool ok = false;
  if (want[0] != 0) {
    const char* path = "/car_skin";
    if (gHeartbeatTryLan && lanEnabled()) {
      WiFiClient c;
      LWIP_LOCK();
      bool con = c.connect(MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 800);
      LWIP_UNLOCK();
      if (con) {
        c.setTimeout(500);
        via = 'L';
        ok = getHttpToBuf(c, MARVIN_LOCAL_HOST, path, false, g_skinBuf, SKIN_BYTES, &n, &code, 6000);
        LWIP_LOCK();
        c.stop();
        LWIP_UNLOCK();
      }
    }
    if (code <= 0) {
      if (xSemaphoreTake(gFunnelMutex, pdMS_TO_TICKS(2000)) == pdTRUE) {
        via = 'F';
        if (funnelEnsureConnected(8, 8000)) {
          ok = getHttpToBuf(gHeartbeatFunnelClient, MARVIN_HOST, path, true, g_skinBuf, SKIN_BYTES, &n, &code, 12000);
          if (!ok) { LWIP_LOCK(); gHeartbeatFunnelClient.stop(); LWIP_UNLOCK(); }
        }
        xSemaphoreGive(gFunnelMutex);
      } else {
        Serial.println("[Skin] Funnel 鎖忙，稍後重試");
        skinScheduleRetry();
        return;
      }
    }
  }

  bool handed = false;
  if (want[0] == 0 || (ok && code == 404)) handed = skinHandOff(nullptr);   // 回預設頭
  else if (ok && code == 200 && n == SKIN_BYTES) handed = skinHandOff(g_skinBuf);
  if (handed) {
    strcpy(g_skinHave, want);
    g_skinHaveValid = true;
    g_skinRetries = 0;
  } else {
    skinScheduleRetry();
  }
  Serial.printf("[Skin] HTTP %d ok=%d %uB %ums via=%c want=%s handed=%d\n",
                code, (int)ok, (unsigned)n, (unsigned)(millis() - t0), via, want, (int)handed);
}
#endif

// 獨立任務，釘死 core 0（跟 audioNetworkTask 同核，理由見 carHeartbeat() 前的註解）。
// 開機立刻打一次（不等第一輪 30s），之後每 30s 一次。
//
// 2026-08-12 補：connectWiFi() 過去只在 setup() 跑一次，斷線後所有 task 只是輪詢
// WiFi.status()==WL_CONNECTED 空轉、從沒再呼叫 wifiMulti.run()，車上斷線後只能
// 物理重開機才有機會重連。這裡改成斷線時每 3s 主動重試（wifiMulti.run() 內部只在
// 真的不是 WL_CONNECTED 時才重新掃描/連線，不會白白重跑），恢復後改回 30s 心跳節奏、
// 燈號跟著回報。⚠️ 這只治得了「WiFi 訊號本身還在、ESP32 端沒去搶救」——如果是
// iPhone個人熱點自己因閒置進入省電把 Wi-Fi 電台關了，重連會一直失敗，需要在手機那邊
// 把「個人熱點」畫面打開喚醒熱點（見碰車puck連線問題前讀的記憶）。
void carHeartbeatTask(void* pv) {
  for (;;) {
    if (WiFi.status() == WL_CONNECTED) {
      carHeartbeat();
#if COVER_FETCH
      // 30s 心跳間隔拆成 1s 一格巡邏：換歌旗標或到期重試就下載封面（跟心跳同 task，自然不會撞在一起）。
      for (int i = 0; i < 30 && WiFi.status() == WL_CONNECTED; i++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        if (g_coverDirty) coverFetch(false);
        else if (g_coverRetryAt && (int32_t)(millis() - g_coverRetryAt) >= 0) { g_coverRetryAt = 0; coverFetch(true); }
#if FACE_DISPLAY
        if (g_skinDirty) skinFetch(false);
        else if (g_skinRetryAt && (int32_t)(millis() - g_skinRetryAt) >= 0) { g_skinRetryAt = 0; skinFetch(true); }
#endif
      }
#else
      vTaskDelay(pdMS_TO_TICKS(30000));
#endif
    } else {
      Serial.println("[WiFi] ⚠️ 斷線，嘗試重連...");
      setLed(LED_ERROR);
      wifiMulti.run();
      if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[WiFi] ✅ 重連成功, SSID=%s IP=%s RSSI=%d\n",
                      WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());
        setLed(LED_CONNECTED);
      }
      vTaskDelay(pdMS_TO_TICKS(3000));
    }
  }
}

void postAudio(int nSamples, bool viaPause);  // 前置宣告（hold-to-talk 放開時呼叫）

// 車機 master volume 旋鈕按下＝送 AVRCP PAUSE（車機自己把音量降到 0），再按＝PLAY。
// 把它當 PTT：PAUSE 開始錄音、PLAY 結束送出。只在 loop() task 讀寫
// （pollWheelFromWroom 與 pttHoldToTalk 都在 loop() 裡呼叫），不需要鎖。
// 隱私風險：車機自己也會送 PAUSE（iPhone 來電、切音源、導航插播），所以這種觸發的錄音
// 要在 Mac 端有喚醒詞把關才處理（見 car_http_app.py inject_audio require_wake），
// 不是喚醒詞就丟棄且不記錄文字內容。實體 PTT 按鈕不受影響，維持現行行為。
static bool g_pausePtt = false;

// hold-to-talk：按住 PTT（實體按鈕或車機暫停鍵）期間持續錄音、放開（或再按一次播放鍵）
// 送出。長度自適應，不像固定秒數會切掉長句或錄多餘環境音。每 loop 呼叫一次（非阻塞）。
void pttHoldToTalk() {
  static bool recording = false;
  static int  recCount = 0;
  static bool recViaPause = false;
  bool physical = (digitalRead(PIN_BTN_PTT) == LOW);
  bool pressed = physical || g_pausePtt;

  if (pressed && !recording) {           // ▼ 按下：開錄
    recording = true; recCount = 0;
    recViaPause = !physical;
    i2s_zero_dma_buffer(I2S_MIC_PORT);    // 丟掉按下前 DMA 累積的舊音
    setLed(LED_LISTENING);                // 收聽中：藍燈
    if (recViaPause) Serial.println("[PTT] ▼ 車機暫停鍵，開始錄音（再按一次結束）");
    else             Serial.println("[PTT] ▼ 按下，開始錄音（按住說話）");
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
      if (recCount >= MIN_REC_SAMPLES) { setLed(LED_PLAYING); postAudio(recCount, recViaPause); }
      else { Serial.println("[PTT] 太短（手滑？），忽略不送"); setLed(LED_STANDBY); }
      // 撞上限且是車機暫停鍵觸發：車機還在暫停狀態，不清旗標的話下一圈會立刻再開錄。
      if (full && recViaPause) g_pausePtt = false;
    }
  }
}

// 把 recBuf 前 nSamples 個樣本包成 WAV，POST /audio
void postAudio(int nSamples, bool viaPause) {
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
  char audioPath[96];   // 身分走 query：/audio?device=puck-xxxxxx（Mac 端 MARVIN_CAR_DEVICES 對應）
  if (viaPause) {
    snprintf(audioPath, sizeof(audioPath), "/audio?device=%s&trigger=pause", gDeviceId);
  } else {
    snprintf(audioPath, sizeof(audioPath), "/audio?device=%s", gDeviceId);
  }
  WiFiClient localClient;
  int code = -1;
  if (lanEnabled()) code = postHttp(localClient, MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 1200,
                       audioPath, "audio/wav", wav, wavBytes, 15000);
  if (code <= 0) {
    Serial.println("[POST /audio] 區網打不到，退回 Funnel...");
    // 2026-10-14：改用 gHeartbeatFunnelClient（跟心跳共用），不再自己 new 一個
    // WiFiClientSecure——這塊板子 TLS 只能用內部 RAM，/audio_stream 常駐一條、心跳
    // 常駐一條，硬開第三條會因為內部 RAM 不夠握手失敗（熱點下實測連續 HTTP -1）。
    // gFunnelMutex 保護這條共用連線，避免跟 carHeartbeat() 同時搶著讀寫。
    if (xSemaphoreTake(gFunnelMutex, pdMS_TO_TICKS(10000)) == pdTRUE) {
      Serial.printf("[POST /audio] Funnel 前 internal free=%u largest=%u\n",
                    (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                    (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
      bool alive = funnelEnsureConnected(8, 8000);
      if (alive) {
        int httpCode = -1;
        bool ok = postHttpKeepAlive(gHeartbeatFunnelClient, MARVIN_HOST, audioPath, "audio/wav",
                                     wav, wavBytes, 15000, &httpCode);
        if (!ok) { LWIP_LOCK(); gHeartbeatFunnelClient.stop(); LWIP_UNLOCK(); }
        code = ok ? httpCode : -1;
      } else {
        char eb[100];
        int e = gHeartbeatFunnelClient.lastError(eb, sizeof(eb));
        Serial.printf("[POST /audio] TLS 失敗 err=%d %s\n", e, eb);
        code = -1;
      }
      xSemaphoreGive(gFunnelMutex);
    } else {
      Serial.println("[POST /audio] Funnel 連線忙");
      code = -1;
    }
  }
  Serial.printf("[POST /audio] HTTP %d\n", code);
  free(wav);
  Serial.printf("[StackWM] loopTask after postAudio: %u words\n",
                (unsigned)uxTaskGetStackHighWaterMark(NULL));
  if (code != 200) { setLed(LED_STANDBY); return; }   // 沒送成功＝沒回覆要播，別卡在青燈
  // 車載常駐串流模式：/reply 已停用，回覆會自動從 audioStreamTask 的 /audio_stream 播出。
#if FACE_DISPLAY
  portENTER_CRITICAL(&g_faceInMux);
  g_faceIn.awaitUntilMs = millis() + 10000;   // 跟 LED_PLAYING 的 10s 逾時一致
  portEXIT_CRITICAL(&g_faceInMux);
#endif
  setLed(LED_STANDBY);
}

#if PUCK_MIC_WS
// ========== PUCK_MIC_WS：全時收音上傳 /mic_ws ==========
//
// micCaptureTask（core 1）唯一讀 I2S_MIC_PORT，編 IMA ADPCM 進 PSRAM ring；micWsTask
// （core 0）常駐 WebSocket 連線把 ring 內容送出，並處理 server 送回的 duck/unduck/
// ping/close。PTT（實體鍵/車機暫停鍵）只負責在 micWsTask 連上時插一筆
// {"type":"ptt",...} 文字訊息（見 pttEdgeToWake()），不再自己錄音。

#define MIC_RING_BLOCKS 128   // 4 秒 @ 32ms/block（260 bytes/block，PSRAM ~33KB）

static uint8_t* g_micRing = nullptr;             // PSRAM，MIC_RING_BLOCKS * IMA_BLOCK_BYTES
static volatile uint32_t g_micHead = 0;          // micCaptureTask（producer）專用寫
static volatile uint32_t g_micTail = 0;          // micWsTask（consumer）專用寫
static volatile uint32_t g_micDropped = 0;       // ring 滿了丟棄的 block 數（診斷用）
static volatile bool g_micEncReset = false;      // micWsTask 連線成功時設；micCaptureTask 清
static volatile bool g_micWsUp = false;          // micWsTask 目前是否已連上（pttEdgeToWake 判斷要不要丟棄按鍵）
static volatile uint8_t g_pttWakeSrc = 0;        // 0=無 1=button 2=pause；micWsTask 送出後清
static TaskHandle_t micCaptureTaskHandle = nullptr;
static TaskHandle_t micWsTaskHandle = nullptr;

static WiFiUDP gDuckUdp;
static bool gDuckUdpBegun = false;
static uint32_t gDuckSeq = 0;
static bool gDucked = false;

// 送 duck/unduck UDP 給手機端 DuckListener（iOS，監聽 47800）。只在 micWsTask 裡呼叫，
// 不等 ack——手機沒收到最壞情況是這次沒壓低/沒恢復音量，下一次 duck/unduck 事件會
// 再送一次，不值得為了可靠性加重試邏輯。
static void duckUdpSend(bool duck) {
  if (!gDuckUdpBegun) { gDuckUdp.begin(0); gDuckUdpBegun = true; }
  gDuckSeq++;
  char body[48];
  int n = snprintf(body, sizeof(body), "{\"cmd\":\"%s\",\"seq\":%u}", duck ? "duck" : "unduck", (unsigned)gDuckSeq);
  LWIP_LOCK();
  gDuckUdp.beginPacket(WiFi.gatewayIP(), 47800);
  gDuckUdp.write((const uint8_t*)body, (size_t)n);
  gDuckUdp.endPacket();
  LWIP_UNLOCK();
  gDucked = duck;
  Serial.printf("[Duck] %s seq=%u\n", duck ? "duck" : "unduck", (unsigned)gDuckSeq);
  setLed(duck ? LED_LISTENING : LED_STANDBY);
}

// 套用 Mac 送來的臉部狀態（{"type":"face","state":...}，見 mic_face_parse）。
// LED 燈色跟著切；speaking 旗標只在 FACE_DISPLAY 開時才有意義，寫入要包臨界區
// （g_faceIn 是 faceTask/core 1 跟這裡/core 0 共用的快照，見 g_faceInMux 定義處註解）。
static void applyFaceState(int st) {
  const char* label = "idle";
  bool speaking = false;
  switch (st) {
    case MIC_FACE_LISTEN: setLed(LED_LISTENING); label = "listen"; break;
    case MIC_FACE_THINK:  setLed(LED_PLAYING);   label = "think";  break;
    case MIC_FACE_SPEAK:  setLed(LED_STANDBY);   label = "speak";  speaking = true; break;
    case MIC_FACE_IDLE:   setLed(LED_STANDBY);   label = "idle";   break;
    default: return;
  }
#if FACE_DISPLAY
  portENTER_CRITICAL(&g_faceInMux);
  g_faceIn.speaking = speaking;
  portEXIT_CRITICAL(&g_faceInMux);
#endif
  Serial.printf("[Face] state=%s\n", label);
}

// 唯一讀麥克風的 task：讀滿 512 個 32-bit frame（不滿就繼續讀補滿）、轉 int16、編
// IMA ADPCM 進 ring。ring 滿了（micWsTask 斷線太久追不上）就丟新 block，不覆蓋舊的、
// 也不阻塞等 consumer。
void micCaptureTask(void* pv) {
  Serial.println("[MicWS] micCaptureTask 已啟動");
  ImaAdpcmState st;
  ima_adpcm_reset(&st);
  int32_t raw[IMA_BLOCK_SAMPLES];
  int16_t pcm[IMA_BLOCK_SAMPLES];
  for (;;) {
    if (g_micEncReset) { ima_adpcm_reset(&st); g_micEncReset = false; }

    size_t got = 0;
    while (got < IMA_BLOCK_SAMPLES) {
      size_t n = 0;
      i2s_read(I2S_MIC_PORT, raw + got, (IMA_BLOCK_SAMPLES - got) * sizeof(int32_t), &n, portMAX_DELAY);
      got += n / sizeof(int32_t);
    }
    for (int i = 0; i < IMA_BLOCK_SAMPLES; i++) pcm[i] = (int16_t)(raw[i] >> 16);

    uint32_t head = g_micHead, tail = g_micTail;
    if (head - tail >= MIC_RING_BLOCKS) {
      g_micDropped++;
      continue;   // ring 滿了，丟這個 block（micWsTask 斷線中/追不上）
    }
    uint8_t* slot = g_micRing + (size_t)(head % MIC_RING_BLOCKS) * IMA_BLOCK_BYTES;
    ima_adpcm_encode_block(&st, pcm, slot);
    g_micHead = head + 1;
  }
}

static inline void wsRandomMask(uint8_t mask[4]) {
  uint32_t r = esp_random();
  mask[0] = (uint8_t)r; mask[1] = (uint8_t)(r >> 8); mask[2] = (uint8_t)(r >> 16); mask[3] = (uint8_t)(r >> 24);
}

// 粗判收到的 frame 宣告長度是否超過 limit，不用等完整 frame 到齊才能判斷——
// ws_parse_header() 要完整 payload 都在 buffer 裡才會回 1，壞資料宣告超大長度時會一直
// 回 0（資料不夠）等到天荒地老，這裡只看 header 的長度欄位本身提早踩停。
static bool wsDeclaredTooLarge(const uint8_t* buf, size_t len, size_t limit) {
  if (len < 2) return false;
  uint8_t len7 = buf[1] & 0x7F;
  if (len7 <= 125) return len7 > limit;
  if (len7 == 126) {
    if (len < 4) return false;
    uint16_t l16 = (uint16_t)(((uint16_t)buf[2] << 8) | buf[3]);
    return (size_t)l16 > limit;
  }
  return true;   // 127（64-bit 擴充長度）：puck 不該收到這麼大的控制 frame，直接判過大
}

// 常駐 WebSocket 上傳任務：連線（區網明碼優先，失敗退回 Funnel TLS，跟
// audioNetworkTask/carHeartbeat 同一套鎖紀律與 TLS 共用原則）→ 握手 → 串流迴圈（送
// ring 裡的音訊/PTT，收 duck/unduck/ping/close）→ 斷線重連（backoff）。
void micWsTask(void* pv) {
  Serial.println("[MicWS] micWsTask 已啟動");
  uint32_t backoffMs = 1000;
  uint32_t sentBlocks = 0, duckCount = 0, unduckCount = 0, pingCount = 0, reconnects = 0, faceCount = 0;
  uint32_t lastStatsMs = millis();
  static WiFiClientSecure gMicFunnelClient;

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) { vTaskDelay(pdMS_TO_TICKS(500)); continue; }

    WiFiClient localClient;
    bool useFunnel = false;
    bool connectOk = false;
    if (lanEnabled()) {
      LWIP_LOCK();
      connectOk = localClient.connect(MARVIN_LOCAL_HOST, MARVIN_LOCAL_PORT, 1200);
      LWIP_UNLOCK();
    }
    if (!connectOk) {
      useFunnel = true;
      gMicFunnelClient.setInsecure();
      gMicFunnelClient.setHandshakeTimeout(5);
      LWIP_LOCK();
      connectOk = gMicFunnelClient.connect(MARVIN_HOST, MARVIN_PORT, 5000);
      LWIP_UNLOCK();
    }
    WiFiClient& client = useFunnel ? (WiFiClient&)gMicFunnelClient : localClient;
    if (!connectOk) {
      Serial.println("[MicWS] ⚠️ connect() 失敗（區網+Funnel都失敗）");
      vTaskDelay(pdMS_TO_TICKS(backoffMs));
      backoffMs = (backoffMs * 2 > 30000) ? 30000 : backoffMs * 2;
      continue;
    }
    client.setTimeout(500);
    const char* host = useFunnel ? MARVIN_HOST : MARVIN_LOCAL_HOST;

    uint8_t keyRaw[16];
    for (int i = 0; i < 16; i += 4) {
      uint32_t r = esp_random();
      keyRaw[i] = (uint8_t)r; keyRaw[i + 1] = (uint8_t)(r >> 8);
      keyRaw[i + 2] = (uint8_t)(r >> 16); keyRaw[i + 3] = (uint8_t)(r >> 24);
    }
    char keyB64[32];
    ws_base64_encode(keyRaw, sizeof(keyRaw), keyB64, sizeof(keyB64));

    String req = String("GET /mic_ws?device=") + gDeviceId + "&t=" + MARVIN_TOKEN + " HTTP/1.1\r\n" +
                 "Host: " + host + "\r\n" +
                 "Upgrade: websocket\r\n" +
                 "Connection: Upgrade\r\n" +
                 "Sec-WebSocket-Key: " + keyB64 + "\r\n" +
                 "Sec-WebSocket-Version: 13\r\n\r\n";
    LWIP_LOCK(); client.print(req); LWIP_UNLOCK();

    String status;
    bool handshakeOk = lockedReadLine(client, status, 3000) && status.indexOf(" 101 ") >= 0;
    if (handshakeOk) {
      for (;;) {
        String h;
        if (!lockedReadLine(client, h, 3000)) { handshakeOk = false; break; }
        if (h.length() == 0) break;   // 空行＝標頭結束
      }
    }
    if (!handshakeOk) {
      Serial.printf("[MicWS] 握手失敗：%s\n", status.c_str());
      LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
      vTaskDelay(pdMS_TO_TICKS(backoffMs));
      backoffMs = (backoffMs * 2 > 30000) ? 30000 : backoffMs * 2;
      continue;
    }

    backoffMs = 1000;
    g_micTail = g_micHead;      // 丟掉斷線期間的舊音
    g_micEncReset = true;
    g_micWsUp = true;
    reconnects++;
    Serial.printf("[MicWS] 連上（%s）\n", useFunnel ? "Funnel" : "LAN");

    static uint8_t rxBuf[512];
    static uint8_t txFrame[2080 + 14];
    static uint8_t txPayload[8 * IMA_BLOCK_BYTES];
    size_t rxLen = 0;
    bool connLost = false;

    while (!connLost) {
      bool didWork = false;

      // 送音訊：ring 裡有 >= 8 個 block 就湊成一則 binary frame。
      uint32_t head = g_micHead, tail = g_micTail;
      if (head - tail >= 8) {
        for (int i = 0; i < 8; i++) {
          uint8_t* slot = g_micRing + (size_t)((tail + i) % MIC_RING_BLOCKS) * IMA_BLOCK_BYTES;
          memcpy(txPayload + i * IMA_BLOCK_BYTES, slot, IMA_BLOCK_BYTES);
        }
        uint8_t mask[4]; wsRandomMask(mask);
        size_t frameLen = ws_build_frame(WS_OP_BINARY, txPayload, sizeof(txPayload), mask, txFrame, sizeof(txFrame));
        if (frameLen > 0) {
          LWIP_LOCK();
          size_t written = client.write(txFrame, frameLen);
          LWIP_UNLOCK();
          if (written < frameLen) {
            Serial.println("[MicWS] 送音訊 write 不完整，視為斷線");
            connLost = true;
          } else {
            g_micTail = tail + 8;
            sentBlocks += 8;
          }
        }
        didWork = true;
      }

      // 送 PTT：按鍵邊緣在 pttEdgeToWake() 設旗標，這裡才真的碰網路。
      uint8_t pttSrc = g_pttWakeSrc;
      if (!connLost && pttSrc != 0) {
        g_pttWakeSrc = 0;
        const char* json = (pttSrc == 1) ? "{\"type\":\"ptt\",\"src\":\"button\"}"
                                          : "{\"type\":\"ptt\",\"src\":\"pause\"}";
        uint8_t mask[4]; wsRandomMask(mask);
        size_t frameLen = ws_build_frame(WS_OP_TEXT, (const uint8_t*)json, strlen(json), mask, txFrame, sizeof(txFrame));
        if (frameLen > 0) {
          LWIP_LOCK();
          size_t written = client.write(txFrame, frameLen);
          LWIP_UNLOCK();
          if (written < frameLen) connLost = true;
          else Serial.printf("[MicWS] PTT 送出 src=%s\n", pttSrc == 1 ? "button" : "pause");
        }
        didWork = true;
      }

      // 收：available() 查有無資料，有才進鎖搬（理由見 lockedReadLine 前的 2026-07-26 註解）。
      if (!connLost) {
        LWIP_LOCK();
        int avail = client.available();
        size_t n = 0;
        if (avail > 0 && rxLen < sizeof(rxBuf)) {
          size_t want = sizeof(rxBuf) - rxLen;
          size_t toRead = (size_t)avail < want ? (size_t)avail : want;
          n = client.read(rxBuf + rxLen, toRead);
        }
        LWIP_UNLOCK();
        if (n > 0) { rxLen += n; didWork = true; }
      }

      // 處理 rxBuf 裡湊齊的完整 frame（可能一次收到多個）。
      while (!connLost) {
        WsHeader h;
        int rc = ws_parse_header(rxBuf, rxLen, &h);
        if (rc == 0) {
          if (wsDeclaredTooLarge(rxBuf, rxLen, 400)) {
            Serial.println("[MicWS] frame 宣告長度過大，斷線");
            connLost = true;
          }
          break;
        }
        if (rc < 0) {
          Serial.println("[MicWS] frame 格式錯，斷線");
          connLost = true;
          break;
        }

        size_t frameTotal = h.header_len + (size_t)h.payload_len;
        const uint8_t* payloadPtr = rxBuf + h.header_len;

        if (h.opcode == WS_OP_TEXT) {
          int ctl = mic_ctl_parse((const char*)payloadPtr, (size_t)h.payload_len);
          if (ctl == MIC_CTL_DUCK) { duckUdpSend(true); duckCount++; }
          else if (ctl == MIC_CTL_UNDUCK) { duckUdpSend(false); unduckCount++; }
          else {
            int face = mic_face_parse((const char*)payloadPtr, (size_t)h.payload_len);
            if (face != MIC_FACE_NONE) { applyFaceState(face); faceCount++; }
            else {
              int plen = (int)(h.payload_len < 80 ? h.payload_len : 80);
              Serial.printf("[MicWS] 未知文字訊息: %.*s\n", plen, (const char*)payloadPtr);
            }
          }
        } else if (h.opcode == WS_OP_PING) {
          uint8_t mask[4]; wsRandomMask(mask);
          size_t frameLen = ws_build_frame(WS_OP_PONG, payloadPtr, (size_t)h.payload_len, mask, txFrame, sizeof(txFrame));
          if (frameLen > 0) {
            LWIP_LOCK();
            size_t written = client.write(txFrame, frameLen);
            LWIP_UNLOCK();
            if (written < frameLen) connLost = true;
          }
          pingCount++;
        } else if (h.opcode == WS_OP_CLOSE) {
          uint8_t closePayload[2] = {0, 0};
          size_t closeLen = (h.payload_len >= 2) ? 2 : 0;
          if (closeLen) memcpy(closePayload, payloadPtr, closeLen);
          uint8_t mask[4]; wsRandomMask(mask);
          size_t frameLen = ws_build_frame(WS_OP_CLOSE, closePayload, closeLen, mask, txFrame, sizeof(txFrame));
          if (frameLen > 0) { LWIP_LOCK(); client.write(txFrame, frameLen); LWIP_UNLOCK(); }
          connLost = true;
        }
        // 其他 opcode（CONT/BINARY，server 不會送）忽略。

        memmove(rxBuf, rxBuf + frameTotal, rxLen - frameTotal);
        rxLen -= frameTotal;
      }

      if (!connLost && !client.connected()) connLost = true;
      if (connLost) break;

      uint32_t now = millis();
      if (now - lastStatsMs >= 60000) {
        lastStatsMs = now;
        Serial.printf("[MicWS] up=1 sent_blocks=%u dropped=%u ring=%u duck=%u unduck=%u ping=%u "
                      "face=%u reconnects=%u ifree=%u imin=%u stack_hw cap=%u ws=%u\n",
                      (unsigned)sentBlocks, (unsigned)g_micDropped, (unsigned)(g_micHead - g_micTail),
                      (unsigned)duckCount, (unsigned)unduckCount, (unsigned)pingCount, (unsigned)faceCount,
                      (unsigned)reconnects,
                      (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                      (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                      micCaptureTaskHandle ? (unsigned)uxTaskGetStackHighWaterMark(micCaptureTaskHandle) : 0,
                      (unsigned)uxTaskGetStackHighWaterMark(NULL));
      }

      if (!didWork) vTaskDelay(pdMS_TO_TICKS(5));
    }

    LWIP_LOCK(); client.stop(); LWIP_UNLOCK();
    g_micWsUp = false;
    if (gDucked) duckUdpSend(false);   // 別讓手機一直壓低
    applyFaceState(MIC_FACE_IDLE);     // 斷線別讓臉卡在 speak/think
    Serial.println("[MicWS] 斷線");
    vTaskDelay(pdMS_TO_TICKS(backoffMs));
    backoffMs = (backoffMs * 2 > 30000) ? 30000 : backoffMs * 2;
  }
}

// PTT（實體鍵/車機暫停鍵）改成「強制喚醒」：按下的那一刻設旗標，micWsTask 連上時才
// 真的送 {"type":"ptt",...}；不讀 I2S、不呼叫 postAudio（micCaptureTask 是唯一讀麥的
// task）。WS 沒連上時按下就直接丟（不排隊——按鍵用意是「現在馬上」，排隊等重連後
// 才送已經失去時機意義，容易誤觸發跟使用者當下情境無關的喚醒）。
void pttEdgeToWake() {
  static uint32_t physLowSinceMs = 0;
  static bool physDebounced = false;
  bool physNow = (digitalRead(PIN_BTN_PTT) == LOW);
  if (physNow) {
    if (physLowSinceMs == 0) physLowSinceMs = millis();
    if (!physDebounced && millis() - physLowSinceMs >= 50) {
      physDebounced = true;
      if (g_micWsUp) { g_pttWakeSrc = 1; Serial.println("[PTT] ▼ 按鍵（強制喚醒）"); }
      else Serial.println("[PTT] ▼ 按鍵，但 WS 未連上，丟棄");
    }
  } else {
    physLowSinceMs = 0;
    physDebounced = false;
  }

  // g_pausePtt 由 pollWheelFromWroom() 的 PAUSE 按鍵設 true；這裡讀到就立刻清回
  // false，天然形成「false→true 邊緣」一次性觸發（PLAY 鍵不再有意義，見 g_pausePtt
  // 定義前的註解）。
  if (g_pausePtt) {
    if (g_micWsUp) { g_pttWakeSrc = 2; Serial.println("[PTT] ▼ 車機暫停鍵（強制喚醒）"); }
    else Serial.println("[PTT] ▼ 車機暫停鍵，但 WS 未連上，丟棄");
    g_pausePtt = false;
  }
}

// setup() 呼叫：配置 mic ring、建 micCaptureTask/micWsTask。PSRAM 配置失敗就不建兩個
// task，其餘功能照常跑。
static void micWsSetup() {
  g_micRing = (uint8_t*)heap_caps_malloc((size_t)MIC_RING_BLOCKS * IMA_BLOCK_BYTES, MALLOC_CAP_SPIRAM);
  Serial.printf("[MicWS] micRing heap_caps_malloc %s（%u bytes）\n",
                g_micRing ? "成功" : "❌失敗", (unsigned)((size_t)MIC_RING_BLOCKS * IMA_BLOCK_BYTES));
  if (!g_micRing) {
    Serial.println("[MicWS] PSRAM 配置失敗，不啟動收音上傳");
    return;
  }
  // stack 8192：raw[512]×4 + pcm[512]×2 + encode 內的 nibbles[512] 已 ~3.5KB，4096 太緊
  xTaskCreatePinnedToCore(micCaptureTask, "micCapture", 8192, nullptr, 3, &micCaptureTaskHandle, 1);
  xTaskCreatePinnedToCore(micWsTask, "micWs", 8192, nullptr, 2, &micWsTaskHandle, 0);
}
#endif  // PUCK_MIC_WS

#if FACE_BENCH
// ========== FACE_BENCH：頭像即時渲染壓力測試 ==========
// 螢幕還沒到貨，腳位是推定的 GC9A01 座，這裡只當輸出腳打時脈用，不初始化螢幕控制器。
// GPIO45（RST，strapping 腳）/GPIO42（BLK）不碰。
#define FB_PIN_CLK  21
#define FB_PIN_MOSI 47
#define FB_PIN_CS   41
#define FB_PIN_DC   40

static uint8_t* g_fbBase888 = nullptr;
static uint8_t* g_fbOut888 = nullptr;
static FaceRtWork* g_fbWork = nullptr;
static SPIClass fbSpi(FSPI);

static const uint8_t FB_BAYER[16] = {0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5};

// 只推「有變動」的範圍：每 4 列（絕對座標）一帶，帶內 x 範圍＝該帶各列 prev（上一格
// 畫過、已還原成 base）跟 cur（這一格畫到的）範圍的聯集；全空就跳過整帶，不打 SPI。
// 逐列轉 RGB565（4×4 Bayer 抖色，公式照抄 tools/puck_face/face_render.js 的
// to565(d,true)）——沒接螢幕時指令照常打時脈，時間/CPU 開銷跟接了螢幕一樣。
// 回傳值：這次實際推出的像素數（各非空帶寬 × 4 加總），供量測用。
static uint32_t fbPush() {
  static uint8_t fbRow[240 * 2];
  int16_t (&curX0)[240] = g_fbWork->curX0;
  int16_t (&curX1)[240] = g_fbWork->curX1;
  int16_t (&prevX0)[240] = g_fbWork->prevX0;
  int16_t (&prevX1)[240] = g_fbWork->prevX1;

  bool txOpen = false;
  uint32_t pushPx = 0;

  for (int by = 0; by < 240; by += 4) {
    int bandX0 = 240, bandX1 = -1;
    for (int y = by; y < by + 4; y++) {
      if (prevX1[y] >= prevX0[y]) {
        if (prevX0[y] < bandX0) bandX0 = prevX0[y];
        if (prevX1[y] > bandX1) bandX1 = prevX1[y];
      }
      if (curX1[y] >= curX0[y]) {
        if (curX0[y] < bandX0) bandX0 = curX0[y];
        if (curX1[y] > bandX1) bandX1 = curX1[y];
      }
    }
    if (bandX1 < bandX0) continue;  // 這帶完全沒變，整帶跳過

    if (!txOpen) {
      fbSpi.beginTransaction(SPISettings(40000000, MSBFIRST, SPI_MODE0));
      digitalWrite(FB_PIN_CS, LOW);
      txOpen = true;
    }

    int by1 = by + 3;

    digitalWrite(FB_PIN_DC, LOW);
    fbSpi.transfer(0x2A);
    digitalWrite(FB_PIN_DC, HIGH);
    uint8_t colBuf[4] = {(uint8_t)(bandX0 >> 8), (uint8_t)(bandX0 & 0xFF), (uint8_t)(bandX1 >> 8), (uint8_t)(bandX1 & 0xFF)};
    fbSpi.writeBytes(colBuf, 4);

    digitalWrite(FB_PIN_DC, LOW);
    fbSpi.transfer(0x2B);
    digitalWrite(FB_PIN_DC, HIGH);
    uint8_t rowBuf[4] = {(uint8_t)(by >> 8), (uint8_t)(by & 0xFF), (uint8_t)(by1 >> 8), (uint8_t)(by1 & 0xFF)};
    fbSpi.writeBytes(rowBuf, 4);

    digitalWrite(FB_PIN_DC, LOW);
    fbSpi.transfer(0x2C);
    digitalWrite(FB_PIN_DC, HIGH);

    int w = bandX1 - bandX0 + 1;
    for (int y = by; y <= by1; y++) {
      int64_t _fbConvT0 = esp_timer_get_time();
      for (int x = bandX0; x <= bandX1; x++) {
        size_t i = (size_t)(y * 240 + x) * 3;
        int r8 = g_fbOut888[i], g8 = g_fbOut888[i + 1], b8 = g_fbOut888[i + 2];
        int k = FB_BAYER[(y & 3) * 4 + (x & 3)];
        // floor(v/255*31 + k/16 - 0.5 + 0.5) = floor((v*31*16 + k*255)/(255*16))，跟原浮點公式數學上相等
        int R5 = (r8 * 496 + k * 255) / 4080;
        int G6 = (g8 * 1008 + k * 255) / 4080;
        int B5 = (b8 * 496 + k * 255) / 4080;
        uint16_t v = (uint16_t)((R5 << 11) | (G6 << 5) | B5);
        int col = x - bandX0;
        fbRow[col * 2] = (uint8_t)(v >> 8);
        fbRow[col * 2 + 1] = (uint8_t)(v & 0xFF);
      }
      int64_t _fbConvT1 = esp_timer_get_time();
      fbSpi.writeBytes(fbRow, w * 2);
      int64_t _fbSpiT1 = esp_timer_get_time();
      g_fbConvUs += (uint32_t)(_fbConvT1 - _fbConvT0);
      g_fbSpiUs += (uint32_t)(_fbSpiT1 - _fbConvT1);
    }

    pushPx += (uint32_t)w * 4;
  }

  if (txOpen) {
    digitalWrite(FB_PIN_CS, HIGH);
    fbSpi.endTransaction();
  }

  return pushPx;
}

// ========== FaceProf：單一運算微基準 + 渲染分段計時（只跑一次，印完就丟）==========
// 跟 face_rt.h 用一樣的編譯選項（O2+fast-math），量出來的 cycles 才跟 render 裡的實際成本可比。
#pragma GCC push_options
#pragma GCC optimize("O2", "fast-math")

static void faceProfBenchOps() {
  const int N = 20000;
  volatile float acc = 0.0f;
  uint32_t c0, c1;

  volatile float x = 0.1f;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { x += 1e-4f; acc = x; }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=base cycles=%.1f\n", (c1 - c0) / (double)N);

  volatile float a = 0.1f, b = 0.2f, cc = 0.3f;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { a += 1e-4f; acc = a * b + cc; }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=fma cycles=%.1f\n", (c1 - c0) / (double)N);

  a = 0.1f;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { a += 1e-4f; acc = a / b; }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=div cycles=%.1f\n", (c1 - c0) / (double)N);

  a = 0.1f;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { a += 1e-4f; acc = sqrtf(a); }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=sqrt cycles=%.1f\n", (c1 - c0) / (double)N);

  a = 0.0f;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { a += 1e-4f; if (a > 0.9f) a = 0.0f; acc = face_rt_detail::fast_asin(a); }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=fast_asin cycles=%.1f\n", (c1 - c0) / (double)N);

  a = 0.0f;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { a += 1e-4f; if (a > 0.9f) a = 0.0f; acc = asinf(a); }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=asinf cycles=%.1f\n", (c1 - c0) / (double)N);

  a = 0.1f;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { a += 1e-4f; acc = sinf(a); }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=sinf cycles=%.1f\n", (c1 - c0) / (double)N);

  volatile uint32_t bacc = 0;
  const size_t bufSize = (size_t)240 * 240 * 3;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { bacc += g_fbBase888[i % bufSize]; }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=psram_rd cycles=%.1f\n", (c1 - c0) / (double)N);

  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { g_fbOut888[i % bufSize] = (uint8_t)i; }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=psram_wr cycles=%.1f\n", (c1 - c0) / (double)N);

  static uint8_t buf[4096];
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) { bacc += buf[i & 4095]; }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=iram_rd cycles=%.1f\n", (c1 - c0) / (double)N);

  const size_t npix = bufSize / 3;
  c0 = esp_cpu_get_cycle_count();
  for (int i = 0; i < N; i++) {
    size_t px = (size_t)(i % npix) * 3;
    uint8_t r8 = g_fbOut888[px], g8 = g_fbOut888[px + 1], b8 = g_fbOut888[px + 2];
    g_fbOut888[px] = r8; g_fbOut888[px + 1] = g8; g_fbOut888[px + 2] = b8;
  }
  c1 = esp_cpu_get_cycle_count();
  Serial.printf("[FaceProf] op=psram_px_rmw cycles=%.1f\n", (c1 - c0) / (double)N);

  (void)acc; (void)bacc;
}

#pragma GCC pop_options

static void faceProfileRender() {
  memset(&g_face_rt_prof, 0, sizeof(g_face_rt_prof));
  const int NFRAMES = 250;

  // face_rt_render() 的不變式：呼叫前 out 要整張等於 base；這裡跑完接著讓 faceBenchTask
  // 用同一塊 out，所以剖析結束後也要再 memcpy 一次並重設 span（見下方）。
  memcpy(g_fbOut888, g_fbBase888, 240 * 240 * 3);
  face_rt_init(g_fbWork);

  int64_t wallT0 = esp_timer_get_time();
  for (int i = 0; i < NFRAMES; i++) {
    float t = i * 0.04f;
    FacePose p = { 1.2f*sinf(0.7f*t), 0.4f*sinf(1.1f*t+1.0f), 0.3f*sinf(0.9f*t+2.0f), 0.03f*(1.0f+sinf(0.5f*t)), 0.0f };
    face_rt_render(p, g_fbBase888, g_fbOut888);
    vTaskDelay(1);   // 讓出 1 tick 餵 task watchdog（連續 250 格約 5s 不讓出會觸發重開機）
  }
  int64_t wallT1 = esp_timer_get_time();

  memcpy(g_fbOut888, g_fbBase888, 240 * 240 * 3);
  face_rt_init(g_fbWork);

  uint32_t frames = g_face_rt_prof.frames ? g_face_rt_prof.frames : 1;
  double prepareAvg = g_face_rt_prof.prepare / (double)frames;
  double copyAvg = g_face_rt_prof.copy / (double)frames;
  double tilesAvg = g_face_rt_prof.tiles / (double)frames;
  double pixelsAvg = g_face_rt_prof.pixels / (double)frames;
  double totalAvg = prepareAvg + copyAvg + tilesAvg + pixelsAvg;
  double tilesTotalAvg = g_face_rt_prof.tiles_total / (double)frames;
  double tilesMaskedAvg = g_face_rt_prof.tiles_masked / (double)frames;
  double pxMathAvg = g_face_rt_prof.px_math / (double)frames;
  double cyclesPerPxMath = g_face_rt_prof.px_math ? g_face_rt_prof.pixels / (double)g_face_rt_prof.px_math : 0;
  double cyclesPerTile = g_face_rt_prof.tiles_total ? g_face_rt_prof.tiles / (double)g_face_rt_prof.tiles_total : 0;
  double tileBboxAvg = g_face_rt_prof.tile_bbox / (double)frames;
  double tileCenterAvg = g_face_rt_prof.tile_center / (double)frames;
  double tileCornerAvg = g_face_rt_prof.tile_corner / (double)frames;
  double tilesLinearAvg = g_face_rt_prof.tiles_linear / (double)frames;
  double tilesExactAvg = g_face_rt_prof.tiles_exact / (double)frames;

  Serial.printf("[FaceProf] frames=%u avg_cycles: prepare=%.0f copy=%.0f tiles=%.0f pixels=%.0f total=%.0f\n",
                (unsigned)frames, prepareAvg, copyAvg, tilesAvg, pixelsAvg, totalAvg);
  Serial.printf("[FaceProf] avg tiles_total=%.1f tiles_masked=%.1f px_math=%.1f cycles_per_px_math=%.1f cycles_per_tile=%.1f\n",
                tilesTotalAvg, tilesMaskedAvg, pxMathAvg, cyclesPerPxMath, cyclesPerTile);
  Serial.printf("[FaceProf] avg_cycles tile_bbox=%.0f tile_center=%.0f tile_corner=%.0f  avg tiles_linear=%.1f tiles_exact=%.1f\n",
                tileBboxAvg, tileCenterAvg, tileCornerAvg, tilesLinearAvg, tilesExactAvg);

  double wallAvgMs = (wallT1 - wallT0) / 1000.0 / NFRAMES;
  Serial.printf("[FaceProf] render_wall_avg=%.3fms\n", wallAvgMs);
}

static void faceProfileRun() {
  faceProfBenchOps();
  faceProfileRender();
  Serial.println("[FaceProf] done");
}

// 固定 24fps 推格；ON/OFF 各 180 秒輪替（偶數 phase=OFF 基準、奇數 phase=ON 真推＋真量）。
// 優先權 1（低於 audioPlay 的 2）：算不完只會掉格，不會搶解碼。
static void faceBenchTask(void* pv) {
  const int64_t PERIOD_US = 41667;
  const int64_t PHASE_US = 180LL * 1000000LL;
  const int64_t LOG_US = 10LL * 1000000LL;

  int phase = 0;
  int64_t phaseStart = esp_timer_get_time();
  int64_t windowStart = phaseStart;
  int64_t next = esp_timer_get_time();

  // 10 秒視窗統計
  uint32_t late = 0, frames = 0, renderMax = 0, pushMax = 0;
  uint64_t renderSum = 0, pushSum = 0, pxSum = 0, pushPxSum = 0;

  // 180 秒 phase 累計（視窗印完歸零前先併進這裡）
  uint32_t pLate = 0, pFrames = 0, pRenderMax = 0, pPushMax = 0;
  uint64_t pRenderSum = 0, pPushSum = 0, pPxSum = 0, pPushPxSum = 0;
  uint32_t pDecMax = 0, pRingEmpty = 0, pRingMin = 0xFFFFFFFF;
  uint64_t pDecUs = 0;
  uint64_t pConvUs = 0, pSpiUs = 0;

  // 把目前視窗累計併進 phase 累計再歸零；視窗自然到期、或 phase 提早結束時都要呼叫，
  // 否則 phase 結束時最後一個還沒滿 10 秒的視窗會漏到下一個 phase。
  auto mergeWindowIntoPhase = [&](int64_t t) {
    pLate += late; pFrames += frames; pRenderSum += renderSum; pPushSum += pushSum; pPxSum += pxSum;
    pPushPxSum += pushPxSum;
    if (renderMax > pRenderMax) pRenderMax = renderMax;
    if (pushMax > pPushMax) pPushMax = pushMax;
    pDecUs += g_fbDecUs;
    if (g_fbDecMaxUs > pDecMax) pDecMax = g_fbDecMaxUs;
    pRingEmpty += g_fbRingEmpty;
    if (g_fbRingMin < pRingMin) pRingMin = g_fbRingMin;
    pConvUs += g_fbConvUs;
    pSpiUs += g_fbSpiUs;
    late = 0; frames = 0; renderSum = 0; pushSum = 0; renderMax = 0; pushMax = 0; pxSum = 0; pushPxSum = 0;
    g_fbDecUs = 0; g_fbDecMaxUs = 0; g_fbRingEmpty = 0; g_fbRingMin = 0xFFFFFFFF;
    g_fbConvUs = 0; g_fbSpiUs = 0;
    windowStart = t;
  };

  faceProfileRun();

  for (;;) {
    bool on = (phase % 2) == 1;

    if (on) {
      float t = esp_timer_get_time() / 1e6f;
      FacePose p = { 1.2f*sinf(0.7f*t), 0.4f*sinf(1.1f*t+1.0f), 0.3f*sinf(0.9f*t+2.0f), 0.03f*(1.0f+sinf(0.5f*t)), 0.0f };
      int64_t t0 = esp_timer_get_time();
      FaceRect r = face_rt_render(p, g_fbBase888, g_fbOut888);
      int64_t t1 = esp_timer_get_time();
      uint32_t pushPx = fbPush();
      int64_t t2 = esp_timer_get_time();
      uint32_t rUs = (uint32_t)(t1 - t0);
      uint32_t pUs = (uint32_t)(t2 - t1);
      frames++; renderSum += rUs; pushSum += pUs;
      if (rUs > renderMax) renderMax = rUs;
      if (pUs > pushMax) pushMax = pUs;
      pxSum += (uint64_t)(r.x1 - r.x0 + 1) * (uint64_t)(r.y1 - r.y0 + 1);
      pushPxSum += pushPx;
    }

    int64_t now = esp_timer_get_time();
    if (now - windowStart >= LOG_US) {
      double winSec = (now - windowStart) / 1e6;
      double renderAvgMs = frames ? (renderSum / (double)frames) / 1000.0 : 0;
      double pushAvgMs = frames ? (pushSum / (double)frames) / 1000.0 : 0;
      double pxAvg = frames ? (double)pxSum / frames : 0;
      double pushPxAvg = frames ? (double)pushPxSum / frames : 0;
      double load = winSec > 0 ? (renderSum + pushSum) / 1e6 / winSec * 100.0 : 0;
      double decLoad = winSec > 0 ? g_fbDecUs / 1e6 / winSec * 100.0 : 0;
      double convAvgMs = frames ? (g_fbConvUs / (double)frames) / 1000.0 : 0;
      double spiAvgMs = frames ? (g_fbSpiUs / (double)frames) / 1000.0 : 0;

      char ringMinStr[16];
      if (g_fbRingMin == 0xFFFFFFFF) snprintf(ringMinStr, sizeof(ringMinStr), "-");
      else snprintf(ringMinStr, sizeof(ringMinStr), "%u", (unsigned)g_fbRingMin);

      Serial.printf("[FaceBench] phase=%s#%d frames=%u late=%u render_avg=%.2fms render_max=%.2fms "
                    "push_avg=%.2fms push_max=%.2fms conv_avg=%.2fms spi_avg=%.2fms px_avg=%.0f push_px_avg=%.0f load=%.1f%% dec_load=%.1f%% "
                    "dec_max=%.2fms ring_empty=%u ring_min=%s ifree=%u imin=%u stack_hw=%u\n",
                    on ? "ON" : "OFF", phase, (unsigned)frames, (unsigned)late,
                    renderAvgMs, renderMax / 1000.0, pushAvgMs, pushMax / 1000.0,
                    convAvgMs, spiAvgMs,
                    pxAvg, pushPxAvg, load, decLoad, g_fbDecMaxUs / 1000.0,
                    (unsigned)g_fbRingEmpty, ringMinStr,
                    (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                    (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                    (unsigned)uxTaskGetStackHighWaterMark(nullptr));

      mergeWindowIntoPhase(now);
    }

    if (now - phaseStart >= PHASE_US) {
      mergeWindowIntoPhase(now);  // 收尾還沒滿 10 秒的那個視窗，不漏到下一個 phase

      double phSec = (now - phaseStart) / 1e6;
      double renderAvgMs = pFrames ? (pRenderSum / (double)pFrames) / 1000.0 : 0;
      double pushAvgMs = pFrames ? (pPushSum / (double)pFrames) / 1000.0 : 0;
      double pxAvg = pFrames ? (double)pPxSum / pFrames : 0;
      double pushPxAvg = pFrames ? (double)pPushPxSum / pFrames : 0;
      double load = phSec > 0 ? (pRenderSum + pPushSum) / 1e6 / phSec * 100.0 : 0;
      double decLoad = phSec > 0 ? pDecUs / 1e6 / phSec * 100.0 : 0;
      double convAvgMs = pFrames ? (pConvUs / (double)pFrames) / 1000.0 : 0;
      double spiAvgMs = pFrames ? (pSpiUs / (double)pFrames) / 1000.0 : 0;

      char ringMinStr[16];
      if (pRingMin == 0xFFFFFFFF) snprintf(ringMinStr, sizeof(ringMinStr), "-");
      else snprintf(ringMinStr, sizeof(ringMinStr), "%u", (unsigned)pRingMin);

      Serial.printf("[FaceBench] === phase=%s#%d frames=%u late=%u render_avg=%.2fms render_max=%.2fms "
                    "push_avg=%.2fms push_max=%.2fms conv_avg=%.2fms spi_avg=%.2fms px_avg=%.0f push_px_avg=%.0f load=%.1f%% dec_load=%.1f%% "
                    "dec_max=%.2fms ring_empty=%u ring_min=%s ifree=%u imin=%u stack_hw=%u\n",
                    on ? "ON" : "OFF", phase, (unsigned)pFrames, (unsigned)pLate,
                    renderAvgMs, pRenderMax / 1000.0, pushAvgMs, pPushMax / 1000.0,
                    convAvgMs, spiAvgMs,
                    pxAvg, pushPxAvg, load, decLoad, pDecMax / 1000.0,
                    (unsigned)pRingEmpty, ringMinStr,
                    (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                    (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                    (unsigned)uxTaskGetStackHighWaterMark(nullptr));

      phase++;
      phaseStart = now;
      pLate = 0; pFrames = 0; pRenderSum = 0; pPushSum = 0; pRenderMax = 0; pPushMax = 0; pPxSum = 0; pPushPxSum = 0;
      pDecUs = 0; pDecMax = 0; pRingEmpty = 0; pRingMin = 0xFFFFFFFF;
      pConvUs = 0; pSpiUs = 0;
    }

    next += PERIOD_US;
    now = esp_timer_get_time();
    if (now >= next) {
      late++;
      next = now;
      vTaskDelay(1);   // 落後時也至少讓出 1 tick，避免長期不讓出觸發 task watchdog
    } else {
      int64_t waitUs = next - now;
      vTaskDelay(pdMS_TO_TICKS((waitUs + 999) / 1000));
    }
  }
}
#endif  // FACE_BENCH

#if FACE_DISPLAY
// ========== FACE_DISPLAY：頭像即時渲染正式整合 ==========
// 顯示法（RGB888→RGB565 + 4×4 Bayer 抖色、只推 prev∪cur dirty 範圍）照抄
// firmware/face_demo/face_demo.ino 的 pushRows/pushDirty，公式不變。
static uint16_t g_faceRow[240];
static const uint8_t FACE_BAYER[16] = {0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5};

static inline uint16_t faceTo565(const uint8_t* p, int x, int y) {
  int k = FACE_BAYER[(y & 3) * 4 + (x & 3)];
  int R5 = (p[0] * 496 + k * 255) / 4080, G6 = (p[1] * 1008 + k * 255) / 4080, B5 = (p[2] * 496 + k * 255) / 4080;
  return (uint16_t)((R5 << 11) | (G6 << 5) | B5);
}

static void facePushRows(int y0, int y1, int x0, int x1) {
  int w = x1 - x0 + 1;
  g_faceTft.setAddrWindow(x0, y0, w, y1 - y0 + 1);
  for (int y = y0; y <= y1; y++) {
    const uint8_t* src = g_faceOut + (size_t)(y * 240 + x0) * 3;
    for (int i = 0; i < w; i++) g_faceRow[i] = faceTo565(src + i * 3, x0 + i, y);
    g_faceTft.writePixels(g_faceRow, w, true);
  }
}

static void facePushFull() {
  g_faceTft.startWrite();
  facePushRows(0, 239, 0, 239);
  g_faceTft.endWrite();
}

static void facePushDirty() {
  g_faceTft.startWrite();
  for (int by = 0; by < 240; by += 4) {
    int bx0 = 240, bx1 = -1;
    for (int y = by; y < by + 4; y++) {
      if (g_faceWork->prevX1[y] >= g_faceWork->prevX0[y]) {
        if (g_faceWork->prevX0[y] < bx0) bx0 = g_faceWork->prevX0[y];
        if (g_faceWork->prevX1[y] > bx1) bx1 = g_faceWork->prevX1[y];
      }
      if (g_faceWork->curX1[y] >= g_faceWork->curX0[y]) {
        if (g_faceWork->curX0[y] < bx0) bx0 = g_faceWork->curX0[y];
        if (g_faceWork->curX1[y] > bx1) bx1 = g_faceWork->curX1[y];
      }
    }
    if (bx1 >= bx0) facePushRows(by, by + 3, bx0, bx1);
  }
  g_faceTft.endWrite();
}

// 釘 core 1、優先權 1，41.667ms 固定節拍（24fps）。落後就重新對齊（不追格），同
// face_demo.ino loop() 的寫法：用 esp_timer 微秒累計，不用 vTaskDelayUntil。
// task 內絕對不呼叫 Serial（HWCDC 底下可能阻塞，見檔頭 STREAM_DEBUG_PRINT 註解）。
void faceTask(void* pv) {
  using namespace face_motion;

  FaceMixer mixer;
  face_mixer_init(mixer);
  FaceIdleGestures idle;
  face_idle_init(idle);
  FaceCoverState cover;
  face_cover_init(cover);
  FaceWake wake;
  face_wake_init(wake);
#if COVER_FETCH
  FaceSkinState skinSt;
  face_skin_init(skinSt);
  uint32_t lastSkinSeq = 0;
  bool skinPending = false;
#endif

  uint32_t lastCoverSeq = 0;
  float idleSinceSec = 0.0f;
  uint8_t prevRawBl = 0xFF;   // sentinel：強制第一格走「resume」路徑，先畫一次底圖

  const int64_t PERIOD_US = 41667;
  const float DT = PERIOD_US / 1e6f;
  int64_t next = esp_timer_get_time();

  for (;;) {
    float T = esp_timer_get_time() / 1e6f;

    FaceInSnapshot snap;
    portENTER_CRITICAL(&g_faceInMux);
    snap = g_faceIn;
    portEXIT_CRITICAL(&g_faceInMux);

    uint32_t nowMs = millis();
    bool streamFresh = (nowMs - snap.lastIcyAtMs <= 15000);
    bool playing = snap.playing && streamFresh;
    bool speaking = snap.speaking && streamFresh;
    if (snap.pendingGesture >= 0) {
      bool expired = (nowMs - snap.pendingAtMs) > 10000;
      if (speaking || expired) {
        if (speaking && !expired) {
          face_mixer_trigger_gesture(mixer, (face_motion::Gesture)snap.pendingGesture, T,
                                      face_motion::gesture_level_amp(snap.pendingLevel));
          g_faceGestures++;
        }
        portENTER_CRITICAL(&g_faceInMux);
        if (g_faceIn.pendingAtMs == snap.pendingAtMs) g_faceIn.pendingGesture = -1;   // 期間有新的就不要清掉新的
        portEXIT_CRITICAL(&g_faceInMux);
      }
    }
    bool awaiting = snap.awaitUntilMs != 0 && (int32_t)(snap.awaitUntilMs - nowMs) > 0;
    if (speaking && snap.awaitUntilMs != 0) {
      // 開始講話就不再等回應——講話本身就是回應已經到了。
      portENTER_CRITICAL(&g_faceInMux);
      g_faceIn.awaitUntilMs = 0;
      portEXIT_CRITICAL(&g_faceInMux);
    }

    FaceInputs in;
    in.override_ = face_motion::resolve_override(snap.override_, speaking, awaiting);
    in.playing = playing;
    in.bpm = snap.bpm;
    in.dur_s = snap.durMs / 1000.0f;
    float posMs;
    if (playing) {
      posMs = (float)snap.posMs + (float)(nowMs - snap.posAtMs);
      if (posMs < 0) posMs = 0;
      if (snap.durMs > 0 && posMs > (float)snap.durMs) posMs = (float)snap.durMs;
    } else {
      posMs = (float)snap.posMs;
    }
    in.pos_s = posMs / 1000.0f;

    idleSinceSec = playing ? 0.0f : idleSinceSec + DT;

    float rand01 = (float)esp_random() / 4294967295.0f;
    int gid = face_idle_step(idle, in, snap.trackKey, T, rand01);
    if (gid >= 0) face_mixer_trigger_gesture(mixer, (Gesture)gid, T);

    FacePoseOut poseOut = face_mixer_step(mixer, in, T, DT);

    // 開機甦醒：第一個聲音互動（Marvin 講話 / 有歌 / 駕駛按 PTT）才醒，見 face_wake_step。
    bool wakeTrigger = speaking || playing || snap.override_ == OVERRIDE_LISTEN;
    FaceWakeOut wakeOut = face_wake_step(wake, wakeTrigger, in.override_ == OVERRIDE_ERROR, poseOut.pose, T);
    poseOut.pose = wakeOut.pose;

    bool overridden = (in.override_ != OVERRIDE_NONE);
    bool coverArrived = (snap.coverSeq != lastCoverSeq);
    if (coverArrived) lastCoverSeq = snap.coverSeq;
    float trackChangedAtS = snap.trackChangedAtMs / 1000.0f;
    FaceCoverOut coverOut = face_cover_step(cover, T, coverArrived, trackChangedAtS, overridden);

    // 網路換底圖：只在頭像在畫面上（COVER_HEAD）時淡出→換→淡入。memcpy 在 render 之前做，
    // out 整張等於新 base，維持 face_rt_render 的不變式；這格強制整屏重推。
    float skinBl = 1.0f;
    bool skinSwapped = false;
#if COVER_FETCH
    if (snap.skinSeq != lastSkinSeq) { lastSkinSeq = snap.skinSeq; skinPending = true; }
    FaceSkinOut skinOut = face_skin_step(skinSt, T, skinPending, coverOut.phase == (int)COVER_HEAD);
    skinBl = skinOut.brightnessScale;
    if (skinOut.swapNow && xSemaphoreTake(gSkinMutex, 0) == pdTRUE) {
      memcpy(g_faceBase, g_skinShowDefault ? BASE_SPHERE : g_skinShow, SKIN_BYTES);
      memcpy(g_faceOut, g_faceBase, SKIN_BYTES);
      xSemaphoreGive(gSkinMutex);
      skinPending = false;
      skinSwapped = true;
    }
#endif

    bool timeValid = time(nullptr) > 1700000000;
    int localHour = 0, localMin = 0;
    if (timeValid) {
      time_t tt = time(nullptr);
      struct tm tmv;
      localtime_r(&tt, &tmv);
      localHour = tmv.tm_hour;
      localMin = tmv.tm_min;
    }
    uint8_t rawBl = face_backlight(timeValid, localHour, localMin, idleSinceSec, overridden);

    if (rawBl == 0) {
      // 背光=0：跳過 render/push，只維持時間與狀態更新（上面已經走完）。
      g_faceTft.setBrightness(0);
      prevRawBl = 0;
    } else {
      if (prevRawBl == 0) facePushFull();   // 從 0 回到 >0：先整屏重推目前 out，再開背光
      uint8_t finalBl = (uint8_t)((float)rawBl * coverOut.brightnessScale * wakeOut.brightnessScale * skinBl + 0.5f);
      bool headVisible = (coverOut.phase != (int)COVER_COVER);

      if (coverOut.drawCoverNow) {
        if (xSemaphoreTake(gCoverMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
          g_faceTft.drawJpg(g_coverShow, g_coverShowLen, 0, 0);
          xSemaphoreGive(gCoverMutex);
        }
      }
      if (headVisible) {
        int64_t rt0 = esp_timer_get_time();
        face_rt_render(poseOut.pose, g_faceBase, g_faceOut);
        int64_t rt1 = esp_timer_get_time();
        if (coverOut.redrawHeadNow || skinSwapped) facePushFull();
        else facePushDirty();
        int64_t rt2 = esp_timer_get_time();
        g_faceRenderUs += (uint64_t)(rt1 - rt0);
        g_facePushUs += (uint64_t)(rt2 - rt1);
      }
      g_faceTft.setBrightness(finalBl);
      prevRawBl = rawBl;
      g_faceFrames++;
    }

    next += PERIOD_US;
    int64_t now = esp_timer_get_time();
    if (now >= next) {
      g_faceLate++;
      next = now;
      vTaskDelay(1);
    } else {
      vTaskDelay(pdMS_TO_TICKS((next - now + 999) / 1000));
    }
  }
}
#endif  // FACE_DISPLAY

// ------------------------------------------------------------------
void setup() {
  loopTaskHandle = xTaskGetCurrentTaskHandle();   // setup()/loop() 是同一顆 Arduino loopTask
  lwipMutex = xSemaphoreCreateMutex();            // 必須在 audioNet/carHeartbeat 任務起來前建好
  gFunnelMutex = xSemaphoreCreateMutex();         // 必須在 carHeartbeatTask 起來前建好（見 gFunnelMutex 前的說明）
  gWroomLogMutex = xSemaphoreCreateMutex();       // 必須在 carHeartbeatTask 起來前建好（見 gWroomLogMutex 前的說明）
#if FACE_DISPLAY
  gCoverMutex = xSemaphoreCreateMutex();          // 必須在 carHeartbeatTask 起來前建好（coverFetch() 會用）
#if COVER_FETCH
  gSkinMutex = xSemaphoreCreateMutex();           // 同上（skinFetch() 會用）
#endif
#endif
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);   // USB 序列埠沒人讀時不要卡住（同 face_demo.ino，避免每幾秒頓一下）
  delay(500);
  Serial.println("\n=== Marvin car_puck bring-up ===");

  format_device_id(ESP.getEfuseMac(), gDeviceId, sizeof(gDeviceId));
  Serial.printf("[boot] device id: %s\n", gDeviceId);

  // 跟 WROOM 的 UART（曲名送出、方向盤按鍵收回），接線見 PIN_WROOM_UART_* 定義前註解。
  // TX buffer 加大到 1024：一筆 metadata 約 400 bytes，預設 128 會讓 audioNetworkTask
  // 在 Serial1.write 裡等 UART 吐完（115200 下約 30ms）；放得下就是純 memcpy 不阻塞。
  Serial1.setTxBufferSize(1024);
  Serial1.begin(115200, SERIAL_8N1, PIN_WROOM_UART_RX, PIN_WROOM_UART_TX);

  setLed(LED_BOOT);
  neopixelWrite(PIN_RGB, 40, 28, 0);   // 立刻亮黃：setup 期間 loop 還沒跑

  // PSRAM 檢查（驗你買對 N16R8）
  Serial.printf("[PSRAM] size = %u bytes（N16R8 應 ~8388608）\n", (unsigned)ESP.getPsramSize());
  if (ESP.getPsramSize() < 4*1024*1024)
    Serial.println("[PSRAM] ❌ 沒偵測到大 PSRAM！確認買的是 N16R8 + Arduino 有開 PSRAM");
  recBuf = (int16_t*)ps_malloc(MAX_REC_SAMPLES * sizeof(int16_t));

#if FACE_DISPLAY
  // 螢幕初始化放在 connectWiFi() 之前（照 face_demo.ino：10MHz 初始化、先關背光、再切
  // 40MHz 畫圖）。PSRAM 配置任何一塊失敗就不建 faceTask，其餘功能照常跑。
  {
    bool tftOk = g_faceTft.init();
    g_faceTft.setBrightness(0);
    g_faceTft.setFreq(40000000);
    g_faceBase = (uint8_t*)ps_malloc(240 * 240 * 3);
    g_faceOut = (uint8_t*)ps_malloc(240 * 240 * 3);
    g_faceWork = (FaceRtWork*)ps_malloc(sizeof(FaceRtWork));
    g_coverShow = (uint8_t*)ps_malloc(COVER_BUF_CAP);
#if COVER_FETCH
    g_skinBuf = (uint8_t*)ps_malloc(SKIN_BYTES);
    g_skinShow = (uint8_t*)ps_malloc(SKIN_BYTES);
    if (!g_skinBuf || !g_skinShow) Serial.println("[Skin] ⚠️ PSRAM 配置失敗，網路換底圖停用");
#endif
    Serial.printf("[Face] tft.init=%d base=%p out=%p work=%p coverShow=%p\n",
                  (int)tftOk, g_faceBase, g_faceOut, g_faceWork, g_coverShow);
    if (!tftOk || !g_faceBase || !g_faceOut || !g_faceWork || !g_coverShow) {
      Serial.println("[Face] ❌ 初始化/PSRAM 配置失敗，不啟動頭像顯示");
    } else {
      memcpy(g_faceBase, BASE_SPHERE, sizeof(BASE_SPHERE));
      memcpy(g_faceOut, g_faceBase, 240 * 240 * 3);
      face_rt_init(g_faceWork);
      g_faceTft.startWrite();
      facePushRows(0, 239, 0, 239);   // 底圖整張推一次（滿足 face_rt_render() 的不變式）
      g_faceTft.endWrite();
      // stack 放 PSRAM（2026-10-25 實測：放內部 RAM 會多吃 8K，害 TLS 握手時 imin 掉到 34K）。
      // 代價：這個 task 不能碰需要關 cache 的操作（寫 flash/NVS）——faceTask 只算圖、推 SPI。
      xTaskCreatePinnedToCoreWithCaps(faceTask, "faceTask", 8192, nullptr, 1, &faceTaskHandle, 1,
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
      Serial.println("[Face] faceTask 啟動");
    }
  }
#endif

  connectWiFi();

  pinMode(PIN_BTN_PTT, INPUT_PULLUP);

  startMic();
#if PUCK_AUDIO_STREAM
  startSpeaker();
  streamRing = (uint8_t*)ps_malloc(STREAM_RING_SIZE);
  Serial.printf("[Stream] streamRing ps_malloc %s（%u bytes）\n",
                streamRing ? "成功" : "❌失敗", (unsigned)STREAM_RING_SIZE);
#endif
#if COVER_FETCH
  g_coverBuf = (uint8_t*)ps_malloc(COVER_BUF_CAP);
  Serial.printf("[Cover] coverBuf ps_malloc %s（%u bytes）\n", g_coverBuf ? "成功" : "❌失敗", (unsigned)COVER_BUF_CAP);
#endif
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
#if PUCK_AUDIO_STREAM
  BaseType_t _rNet = xTaskCreatePinnedToCore(audioNetworkTask, "audioNet", 16384, nullptr, 2, &audioNetTaskHandle, 0);
  BaseType_t _rPlay = xTaskCreatePinnedToCore(audioPlaybackTask, "audioPlay", 8192, nullptr, 2, &audioPlayTaskHandle, 1);
  Serial.printf("[Stream] 任務建立 net=%d play=%d（1=成功）\n", (int)_rNet, (int)_rPlay);
#endif
#if PUCK_MIC_WS
  micWsSetup();
#endif
#if FACE_BENCH
  // 螢幕還沒到貨，先量 face_rt_render()/RGB565/SPI 推圖耗時對 MP3 解碼的排擠（見
  // faceBenchTask 前的說明）。PSRAM 配置失敗就不建 task，不要半殘跑。
  g_fbBase888 = (uint8_t*)heap_caps_malloc(240 * 240 * 3, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  g_fbOut888 = (uint8_t*)heap_caps_malloc(240 * 240 * 3, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  g_fbWork = (FaceRtWork*)heap_caps_malloc(sizeof(FaceRtWork), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!g_fbBase888 || !g_fbOut888 || !g_fbWork) {
    Serial.println("[FaceBench] ❌ PSRAM 配置失敗，不啟動");
  } else {
    for (int y = 0; y < 240; y++) {
      for (int x = 0; x < 240; x++) {
        size_t i = (size_t)(y * 240 + x) * 3;
        g_fbBase888[i]     = (uint8_t)((x * 7 + y * 3) & 255);
        g_fbBase888[i + 1] = (uint8_t)((x * y) & 255);
        g_fbBase888[i + 2] = (uint8_t)((x + 2 * y) & 255);
      }
    }
    memcpy(g_fbOut888, g_fbBase888, 240 * 240 * 3);
    pinMode(FB_PIN_CS, OUTPUT); digitalWrite(FB_PIN_CS, HIGH);
    pinMode(FB_PIN_DC, OUTPUT); digitalWrite(FB_PIN_DC, HIGH);
    fbSpi.begin(FB_PIN_CLK, -1, FB_PIN_MOSI, -1);
    face_rt_init(g_fbWork);
    // 2026-10-24 實測：core 0 實測無效（共用快取/PSRAM，解碼反而變慢、網路重連時 render 卡 500ms），維持 core 1。
    xTaskCreatePinnedToCore(faceBenchTask, "faceBench", 6144, nullptr, 1, nullptr, 1);
    Serial.println("[FaceBench] 啟動（24fps，OFF/ON 各180s輪替）");
  }
#endif
  // 心跳釘死 core 0（跟 audioNetworkTask 同核，理由見 carHeartbeat() 前的註解）；
  // 任務起來後第一輪迴圈立刻打一次，不用等第一輪 30s。隔離實驗（31 分鐘拔掉心跳
  // 零崩潰）已證實心跳是必要條件，方案 A 落地後重新啟用測試。
  // 2026-08-12：優先權從 1 調到 2，跟 audioNet/deckNet 同級——原本=1 時實機常被同核心
  // 的網路 task 餓到搶不到 LWIP_LOCK，心跳連續 HTTP -1（見 carHeartbeat() 前的註解）。
  xTaskCreatePinnedToCore(carHeartbeatTask, "carHeartbeat", 8192, nullptr, 2, nullptr, 0);
  // 收尾定燈：WiFi 通但沒跑 Funnel 檢查也給個 connected 提示；
  // 若前面已 setLed(ERROR/CONNECTED) 則不覆蓋。
  if (WiFi.status() == WL_CONNECTED && ledState == LED_BOOT) setLed(LED_CONNECTED);
  Serial.println("[READY]");
}

// 方向盤按鍵通道：WROOM 收到車機 AVRCP passthru 後經 UART2 轉送過來，這裡非阻塞湊一行
// （'\n' 結尾，上限 192 bytes——"S|" 統計摘要可能到 ~140 字，超長仍丟棄整行）。
// master volume 旋鈕按下＝PAUSE(0x46)、再按＝PLAY(0x44)，當成 PTT 開錄/結束（見
// g_pausePtt）；"D|"/"S|" 開頭是 WROOM 轉送過來的遙測（見 gWroomLogMutex 前的說明），
// 存進暫存給 carHeartbeat() 夾帶出去；其他按鍵只印 log，不做播放控制。
static void pollWheelFromWroom() {
  static char lineBuf[192];
  static size_t lineLen = 0;
  static bool overflow = false;
  while (Serial1.available() > 0) {
    char c = (char)Serial1.read();
    if (c == '\n') {
      if (!overflow && lineLen > 0) {
        lineBuf[lineLen] = '\0';
        if (strncmp(lineBuf, "D|", 2) == 0) {
          const char* text = lineBuf + 2;
          Serial.printf("[WROOM] %s\n", text);
          if (xSemaphoreTake(gWroomLogMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
            if (gWroomDiagN < 8) {
              strncpy(gWroomDiag[gWroomDiagN], text, sizeof(gWroomDiag[0]) - 1);
              gWroomDiag[gWroomDiagN][sizeof(gWroomDiag[0]) - 1] = '\0';
              gWroomDiagN++;
            } else {
              // 滿了就丟最舊的一筆，整體往前移一格再放最後
              for (uint8_t i = 1; i < 8; i++) memcpy(gWroomDiag[i - 1], gWroomDiag[i], sizeof(gWroomDiag[0]));
              strncpy(gWroomDiag[7], text, sizeof(gWroomDiag[0]) - 1);
              gWroomDiag[7][sizeof(gWroomDiag[0]) - 1] = '\0';
            }
            xSemaphoreGive(gWroomLogMutex);
          }
        } else if (strncmp(lineBuf, "S|", 2) == 0) {
          const char* text = lineBuf + 2;
          Serial.printf("[WROOM] %s\n", text);
          if (xSemaphoreTake(gWroomLogMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
            strncpy(gWroomSum, text, sizeof(gWroomSum) - 1);
            gWroomSum[sizeof(gWroomSum) - 1] = '\0';
            gWroomSumNew = true;
            xSemaphoreGive(gWroomLogMutex);
          }
        } else {
          Serial.printf("[Wheel] %s\n", lineBuf);
          if (strcmp(lineBuf, "K|46|p") == 0) g_pausePtt = true;
          else if (strcmp(lineBuf, "K|44|p") == 0) g_pausePtt = false;
        }
      }
      lineLen = 0;
      overflow = false;
      continue;
    }
    if (c == '\r') continue;
    if (lineLen >= sizeof(lineBuf) - 1) {
      overflow = true;
      continue;
    }
    lineBuf[lineLen++] = c;
  }
}

void loop() {
#if PUCK_MIC_WS
  pttEdgeToWake();   // 按鍵/暫停鍵邊緣＝強制喚醒（送 {"type":"ptt"}），不再自己錄音
#else
  pttHoldToTalk();   // 按住說話、放開送出
#endif
  pollWheelFromWroom();   // 方向盤按鍵通道（這階段只印 log）
  // 心跳 2026-07-25 起改由 carHeartbeatTask（core 0 專用任務）驅動，不再從這裡呼叫。
  updateLed();       // 狀態燈動畫（非阻塞）
  delay(5);
}
