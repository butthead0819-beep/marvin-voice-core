/*
 * bt_bridge — car puck 藍牙發射板
 *
 * 用途：把音訊用 Classic Bluetooth A2DP 送到藍牙喇叭。
 *       ESP32-S3 沒有 Classic BT，所以這塊板子另用原版 ESP32-D0WD-V3。
 *       WiFi 不開，所以跟藍牙零共存問題。
 *
 * 目前階段：STEP 1 — 正弦波 smoke test
 *       只驗證藍牙配對 / 重連 / 連續出聲，不接 I2S、不開 WiFi。
 *       目標喇叭：Anker Soundcore Mini 3（名稱比對 "soundcore"，不分大小寫）。
 *
 * 燒錄指令：
 *   arduino-cli compile -b esp32:esp32:esp32 firmware/bt_bridge
 *   arduino-cli upload  -b esp32:esp32:esp32 -p /dev/cu.usbserial-0001 firmware/bt_bridge
 */

#include "BluetoothA2DPSource.h"
#include <math.h>

BluetoothA2DPSource a2dp_source;

// ---- 正弦波產生 ----
static const double SINE_FREQ_HZ = 440.0;
static const double SAMPLE_RATE_HZ = 44100.0;
static const double SINE_AMPLITUDE = 8000.0;
static const double TWO_PI_VAL = 2.0 * M_PI;
static const double PHASE_STEP = TWO_PI_VAL * SINE_FREQ_HZ / SAMPLE_RATE_HZ;
static double sine_phase = 0.0;

int32_t sine_data_cb(Frame* data, int32_t frame_count) {
  for (int32_t i = 0; i < frame_count; i++) {
    int16_t sample = (int16_t)(SINE_AMPLITUDE * sin(sine_phase));
    data[i].channel1 = sample;  // 左
    data[i].channel2 = sample;  // 右
    sine_phase += PHASE_STEP;
    if (sine_phase > TWO_PI_VAL) {
      sine_phase -= TWO_PI_VAL;
    }
  }
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

// ---- 連線狀態 callback ----
void connection_state_cb(esp_a2d_connection_state_t state, void* obj) {
  Serial.printf("[BT] 連線狀態 -> %s\n", a2dp_source.to_str(state));
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[BT] bt_bridge STEP1 boot");

  a2dp_source.set_data_callback_in_frames(sine_data_cb);
  a2dp_source.set_ssid_callback(ssid_match_cb);
  a2dp_source.set_on_connection_state_changed(connection_state_cb);
  a2dp_source.set_auto_reconnect(true);
  a2dp_source.set_volume(60);
  a2dp_source.start();
}

void loop() {
  Serial.printf("[BT] alive connected=%d\n", a2dp_source.is_connected() ? 1 : 0);
  delay(5000);
}
