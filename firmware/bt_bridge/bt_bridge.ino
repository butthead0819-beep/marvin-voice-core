/*
 * bt_bridge — car puck 藍牙發射板
 *
 * 用途：把音訊用 Classic Bluetooth A2DP 送到藍牙喇叭。
 *       ESP32-S3 沒有 Classic BT，所以這塊板子另用原版 ESP32-D0WD-V3。
 *       WiFi 不開，所以跟藍牙零共存問題。
 *
 * 目前階段：STEP 2 — I2S slave 收 S3 音訊 → 48k→44.1k 重取樣 → A2DP
 *       S3 為 I2S master（48000Hz / 16-bit / stereo / Philips），本板並聯收同一組線當 slave。
 *       音量交給 S3 端與喇叭，這裡不衰減。
 *       目標喇叭：Anker Soundcore Mini 3（名稱比對 "soundcore"，不分大小寫）。
 *
 * 接線表（S3 為 master，本板並聯 slave RX）：
 *   S3 GPIO15 (BCLK)  → ESP32 GPIO26
 *   S3 GPIO16 (LRCLK) → ESP32 GPIO25
 *   S3 GPIO7  (DIN)   → ESP32 GPIO22
 *   GND ↔ GND（必接）
 *   PCM5102 照接，不用拔。
 *
 * 燒錄指令：
 *   arduino-cli compile -b esp32:esp32:esp32 firmware/bt_bridge
 *   arduino-cli upload  -b esp32:esp32:esp32 -p /dev/cu.usbserial-0001 firmware/bt_bridge
 */

#include "BluetoothA2DPSource.h"
#include <driver/i2s_std.h>
#include "bridge_dsp.h"

// ---- 首次配對補開重連 ----
// 函式庫在 NVS 沒有上次位址時會整段關掉自動重連，且之後不再打開，這裡補開。
class BridgeSource : public BluetoothA2DPSource {
 public:
  bool arm_reconnect() {  // 回傳是否真的補開了
    if (is_autoreconnect_allowed) return false;
    is_autoreconnect_allowed = true;
    reconnect_status = AutoReconnect;
    reconnect_retries = max_reconnect_retries;
    return true;
  }
};

BridgeSource a2dp_source;

// ---- 音訊管線 ----
static_assert(sizeof(Frame) == sizeof(StereoFrame), "Frame 與 StereoFrame 大小必須相同");

static i2s_chan_handle_t rx = NULL;
static Resampler48to44 g_resampler;
static FrameRing g_ring;
static OutputState g_out_state;
volatile uint32_t g_frames_in = 0;  // 累計 I2S 收到的 48k 格數（reader task 寫）
static volatile bool g_connected_evt = false;

// ---- I2S slave RX ----
static bool i2s_rx_init() {
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
  esp_err_t err = i2s_new_channel(&cc, NULL, &rx);
  if (err != ESP_OK) {
    Serial.printf("[I2S] i2s_new_channel 失敗 err=%d\n", (int)err);
    return false;
  }

  i2s_std_config_t std_cfg = {
    .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(48000),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,
      .bclk = GPIO_NUM_26,
      .ws = GPIO_NUM_25,
      .dout = I2S_GPIO_UNUSED,
      .din = GPIO_NUM_22,
      .invert_flags = {
        .mclk_inv = false,
        .bclk_inv = false,
        .ws_inv = false,
      },
    },
  };

  err = i2s_channel_init_std_mode(rx, &std_cfg);
  if (err != ESP_OK) {
    Serial.printf("[I2S] i2s_channel_init_std_mode 失敗 err=%d\n", (int)err);
    return false;
  }
  err = i2s_channel_enable(rx);
  if (err != ESP_OK) {
    Serial.printf("[I2S] i2s_channel_enable 失敗 err=%d\n", (int)err);
    return false;
  }
  return true;
}

static void i2s_reader_task(void* arg) {
  StereoFrame in[256];
  StereoFrame rs[280];
  while (true) {
    size_t bytes = 0;
    if (i2s_channel_read(rx, in, sizeof(in), &bytes, pdMS_TO_TICKS(50)) != ESP_OK) {
      continue;
    }
    size_t n = bytes / sizeof(StereoFrame);
    g_frames_in += n;
    size_t m = g_resampler.process(in, n, rs, 280);
    g_ring.push(rs, m);
  }
}

// ---- A2DP 資料 callback ----
int32_t bridge_data_cb(Frame* data, int32_t frame_count) {
  fill_output(g_ring, g_out_state, reinterpret_cast<StereoFrame*>(data), (uint32_t)frame_count);
  delay(1);  // 照範例防 watchdog
  return frame_count;
}

// ---- 不分大小寫子字串比對（自寫，不引入其他函式庫）----
static bool contains_ignore_case(const char* haystack, const char* needle) {
  size_t n_len = strlen(needle);
  if (n_len == 0) return true;
  for (const char* h = haystack; *h; h++) {
    size_t i = 0;
    while (i < n_len && h[i] && tolower((unsigned char)h[i]) == tolower((unsigned char)needle[i])) {
      i++;
    }
    if (i == n_len) return true;
  }
  return false;
}

// ---- 名稱比對 callback ----
bool ssid_match_cb(const char* ssid, esp_bd_addr_t address, int rssi) {
  Serial.printf("[BT] 掃到 '%s' rssi=%d\n", ssid, rssi);
  return contains_ignore_case(ssid, "soundcore");
}

// ---- 連線狀態 callback（只設旗標，reconnect 由 loop() 處理）----
void connection_state_cb(esp_a2d_connection_state_t state, void* obj) {
  Serial.printf("[BT] 連線狀態 -> %s\n", a2dp_source.to_str(state));
  if (state == ESP_A2D_CONNECTION_STATE_CONNECTED) {
    g_connected_evt = true;
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[BT] bt_bridge STEP2 boot");

  // I2S 失敗就不起 reader task，只讓 BT 照常跑（靜音），避免空讀迴圈吃光 CPU
  if (i2s_rx_init()) {
    xTaskCreatePinnedToCore(i2s_reader_task, "i2s_rd", 4096, NULL, 10, NULL, 1);
  }

  a2dp_source.set_data_callback_in_frames(bridge_data_cb);
  a2dp_source.set_ssid_callback(ssid_match_cb);
  a2dp_source.set_on_connection_state_changed(connection_state_cb);
  a2dp_source.set_auto_reconnect(true);
  a2dp_source.set_volume(127);
  a2dp_source.start();
}

void loop() {
  static uint32_t last_ms = 0;
  static uint32_t last_frames = 0;

  if (g_connected_evt) {
    g_connected_evt = false;
    if (a2dp_source.arm_reconnect()) {
      Serial.println("[BT] 首次配對：補開自動重連");
    }
  }

  uint32_t now = millis();
  if (now - last_ms >= 5000) {
    uint32_t cur = g_frames_in;
    uint32_t in_rate = (cur - last_frames) / 5;
    last_frames = cur;
    last_ms = now;
    Serial.printf("[BR] conn=%d in_rate=%lu fill=%lu prime=%d under=%lu over=%lu drop=%lu dup=%lu\n",
                  a2dp_source.is_connected() ? 1 : 0,
                  (unsigned long)in_rate,
                  (unsigned long)g_ring.fill(),
                  g_out_state.priming ? 1 : 0,
                  (unsigned long)g_out_state.underruns,
                  (unsigned long)g_ring.overruns,
                  (unsigned long)g_out_state.drops,
                  (unsigned long)g_out_state.dups);
  }
  delay(100);
}
