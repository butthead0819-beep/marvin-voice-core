/*
 * bt_bridge — car puck 藍牙發射板
 *
 * 用途：把音訊用 Classic Bluetooth A2DP 送到藍牙喇叭。
 *       ESP32-S3 沒有 Classic BT，所以這塊板子另用原版 ESP32-D0WD-V3。
 *       WiFi 不開，所以跟藍牙零共存問題。
 *
 * 目前階段：STEP 3 — 多裝置記憶 + 失敗輪替 + BOOT 長按配對 + LED
 *       I2S slave 收 S3 音訊 → 48k→44.1k 重取樣 → A2DP。
 *       S3 為 I2S master（48000Hz / 16-bit / stereo / Philips），本板並聯收同一組線當 slave。
 *       音量交給 S3 端與喇叭，這裡不衰減。
 *       目標裝置：名稱含 "BMW 04900"（車機）或 "soundcore"（Soundcore Mini 3 Pro），不分大小寫。
 *       已配對裝置記在 NVS（最多 4 台，最近連上的排最前）；連線失敗時輪流換下一台。
 *       STEP 3.1 掃描過濾自理（不做 COD 過濾、名字 EIR→BDNAME→遠端名稱）+ 配對診斷紀錄（開機印 [DIAG]）。
 *
 * 使用方式：
 *   配對新裝置＝長按 BOOT 3 秒（LED 快閃）→ BMW 在 iDrive 選「連接新裝置」／喇叭進配對模式
 *   → 連上後 LED 恆亮；之後開機自動輪流重連已知裝置（LED 慢閃，連上恆亮）。
 *
 * 接線表（S3 為 master，本板並聯 slave RX）：
 *   S3 GPIO15 (BCLK)  → ESP32 GPIO26
 *   S3 GPIO16 (LRCLK) → ESP32 GPIO25
 *   S3 GPIO7  (DIN)   → ESP32 GPIO22
 *   GND ↔ GND（必接）
 *   PCM5102 照接，不用拔。
 *   GPIO0 (BOOT) 按鈕：長按 3 秒 = 清除上次裝置、重開進入配對模式
 *   GPIO2 LED：恆亮=已連線 / 快閃=配對中未連線 / 慢閃=重連中未連線
 *
 * 燒錄指令：
 *   arduino-cli compile -b esp32:esp32:esp32 firmware/bt_bridge
 *   arduino-cli upload  -b esp32:esp32:esp32 -p /dev/cu.usbserial-0001 firmware/bt_bridge
 */

#include "BluetoothA2DPSource.h"
#include <driver/i2s_std.h>
#include "bridge_dsp.h"
#include "known_devices.h"
#include "diag_log.h"
#include <nvs.h>

// ---- 配對診斷紀錄的 thread 安全佇列 ----
// BT callback 只把字串丟進這 4 格（portMUX 保護），loop() 取出後寫進 g_diag 並存 NVS。
static const int DIAG_Q = 4;
static char g_diag_q[DIAG_Q][DIAG_TEXT];
static uint8_t g_diag_q_n = 0;
static uint32_t g_diag_q_drop = 0;
static portMUX_TYPE g_diag_mux = portMUX_INITIALIZER_UNLOCKED;
static DiagLog g_diag;

static void diag_post(const char* text) {
  portENTER_CRITICAL(&g_diag_mux);
  if (g_diag_q_n < DIAG_Q) {
    strncpy(g_diag_q[g_diag_q_n], text, DIAG_TEXT - 1);
    g_diag_q[g_diag_q_n][DIAG_TEXT - 1] = '\0';
    g_diag_q_n++;
  } else {
    g_diag_q_drop++;
  }
  portEXIT_CRITICAL(&g_diag_mux);
}

// ---- 掃描用的位址集合（本次開機有效，最多 8 筆；加入成功才回 true）----
static const int ADDR_SET_MAX = 8;
static uint8_t g_seen_a[ADDR_SET_MAX][6];      // 配對模式下已記過 'S' 摘要的裝置
static uint8_t g_seen_n = 0;
static uint8_t g_name_req_a[ADDR_SET_MAX][6];  // 已請求過遠端名稱的裝置
static uint8_t g_name_req_n = 0;

static bool addr_set_add(uint8_t a[][6], uint8_t& n, const uint8_t bda[6]) {
  for (int i = 0; i < n; i++) {
    if (memcmp(a[i], bda, 6) == 0) return false;
  }
  if (n >= ADDR_SET_MAX) return false;
  memcpy(a[n++], bda, 6);
  return true;
}

static void bda_fmt(const uint8_t a[6], char out[18]) {
  snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X", a[0], a[1], a[2], a[3], a[4], a[5]);
}

bool ssid_match_cb(const char* ssid, esp_bd_addr_t address, int rssi);
const char* addr_target_label(const uint8_t* bda);

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
  bool pairing_mode() { return !is_autoreconnect_allowed; }   // 開機無上次位址 → 掃描=配對模式
  void get_last(uint8_t out[6]) { memcpy(out, last_connection, 6); }
  void retarget(const uint8_t a[6]) {
    memcpy(last_connection, a, 6);
    reconnect_retries = max_reconnect_retries;
  }

 protected:
  // 發射功率上限 +3dBm(預設) → +9dBm：開放空間 3 公尺就會斷線（5 公分不會）。
  // esp_bredr_tx_power_set 必須在 controller enable 之後、profile init 之前呼叫，正好是 bt_start() 結束時。
  bool bt_start() override {
    bool ok = BluetoothA2DPSource::bt_start();
    if (ok) {
      esp_err_t err = esp_bredr_tx_power_set(ESP_PWR_LVL_N0, ESP_PWR_LVL_P9);
      Serial.printf("[BT] 發射功率上限 +9dBm err=%d\n", (int)err);
    }
    return ok;
  }

  // 照函式庫「找到目標」的做法：設 DISCOVERED、記位址、取消掃描；
  // 之後 app_gap_callback 在 DISC_STATE STOPPED 看到 DISCOVERED 就 esp_a2d_connect。
  void adopt_target(const uint8_t bda[6], const char* name) {
    strncpy((char*)s_peer_bdname, name, ESP_BT_GAP_MAX_BDNAME_LEN);
    s_peer_bdname[ESP_BT_GAP_MAX_BDNAME_LEN] = '\0';
    s_a2d_state = APP_AV_STATE_DISCOVERED;
    memcpy(peer_bd_addr, bda, 6);
    set_last_connection(peer_bd_addr);
    esp_bt_gap_cancel_discovery();
  }

  // 函式庫會靜默丟掉非 RENDERING/AUDIO COD 與無 EIR 的裝置；這裡不做 COD 過濾，名字 EIR→BDNAME→遠端名稱。
  void filter_inquiry_scan_result(esp_bt_gap_cb_param_t* param) override {
    uint8_t* bda = param->disc_res.bda;
    uint32_t cod = 0;
    int32_t rssi = -129;  // 無效值，同函式庫
    uint8_t* eir = nullptr;
    char prop_name[ESP_BT_GAP_MAX_BDNAME_LEN + 1] = {0};
    for (int i = 0; i < param->disc_res.num_prop; i++) {
      esp_bt_gap_dev_prop_t* p = param->disc_res.prop + i;
      switch (p->type) {
        case ESP_BT_GAP_DEV_PROP_COD:
          cod = *(uint32_t*)(p->val);
          break;
        case ESP_BT_GAP_DEV_PROP_RSSI:
          rssi = *(int8_t*)(p->val);
          break;
        case ESP_BT_GAP_DEV_PROP_EIR:
          eir = (uint8_t*)(p->val);
          break;
        case ESP_BT_GAP_DEV_PROP_BDNAME: {
          int n = p->len;
          if (n > ESP_BT_GAP_MAX_BDNAME_LEN) n = ESP_BT_GAP_MAX_BDNAME_LEN;
          if (n > 0) {
            memcpy(prop_name, p->val, n);
            prop_name[n] = '\0';
          }
          break;
        }
        default:
          break;
      }
    }

    char name[ESP_BT_GAP_MAX_BDNAME_LEN + 1] = {0};
    if (!(eir && get_name_from_eir(eir, (uint8_t*)name, nullptr))) {
      strncpy(name, prop_name, ESP_BT_GAP_MAX_BDNAME_LEN);
    }

    char bs[18];
    bda_fmt(bda, bs);
    Serial.printf("[SCAN] %s cod=0x%06lx rssi=%d eir=%d name='%s'\n", bs, (unsigned long)cod, (int)rssi,
                  eir != nullptr, name);

    if (pairing_mode() && addr_set_add(g_seen_a, g_seen_n, bda)) {
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "S %02X%02X%02X cod%06lx r%d '%s'", bda[3], bda[4], bda[5], (unsigned long)cod,
               (int)rssi, name);
      diag_post(d);
    }

    const char* addr_label = nullptr;
    if (name[0] != '\0') {
      if (ssid_match_cb(name, bda, rssi)) adopt_target(bda, name);
    } else if ((addr_label = addr_target_label(bda)) != nullptr) {
      // BMW 04900 搜尋回應不帶名稱、遠端名稱請求回 stat=1（10/7 [DIAG] 實測），只能靠位址認
      Serial.printf("[SCAN] 位址命中 '%s'（無名稱）%s\n", addr_label, bs);
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "M %02X%02X%02X '%s'", bda[3], bda[4], bda[5], addr_label);
      diag_post(d);
      adopt_target(bda, addr_label);
    } else if (addr_set_add(g_name_req_a, g_name_req_n, bda)) {
      Serial.printf("[SCAN] 無名稱，請求遠端名稱 %s\n", bs);
      esp_bt_gap_read_remote_name(bda);
    }
  }

  void app_gap_callback(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t* param) override {
    if (event == ESP_BT_GAP_READ_REMOTE_NAME_EVT) {
      uint8_t* bda = param->read_rmt_name.bda;
      int stat = param->read_rmt_name.stat;
      const char* name = (const char*)param->read_rmt_name.rmt_name;
      char bs[18];
      bda_fmt(bda, bs);
      Serial.printf("[SCAN] 遠端名稱 %s stat=%d name='%s'\n", bs, stat, name);
      if (pairing_mode()) {
        char d[DIAG_TEXT];
        snprintf(d, sizeof(d), "N %02X%02X%02X st%d '%s'", bda[3], bda[4], bda[5], stat, name);
        diag_post(d);
      }
      if (stat == ESP_BT_STATUS_SUCCESS && ssid_match_cb(name, bda, -1) &&
          s_a2d_state != APP_AV_STATE_DISCOVERED) {
        adopt_target(bda, name);
      }
      return;  // 基底不處理這個事件
    }
    if (event == ESP_BT_GAP_AUTH_CMPL_EVT) {
      uint8_t* bda = param->auth_cmpl.bda;
      int stat = param->auth_cmpl.stat;
      const char* name = (const char*)param->auth_cmpl.device_name;
      char bs[18];
      bda_fmt(bda, bs);
      Serial.printf("[BT] 配對驗證 %s stat=%d name='%s'\n", bs, stat, name);
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "A %02X%02X%02X st%d '%s'", bda[3], bda[4], bda[5], stat, name);
      diag_post(d);
    }
    // SSP 配對過程也記下來（BMW 要求比對數字 / 要我們輸入 passkey / 顯示 passkey）
    if (event == ESP_BT_GAP_CFM_REQ_EVT || event == ESP_BT_GAP_KEY_REQ_EVT || event == ESP_BT_GAP_KEY_NOTIF_EVT) {
      char d[DIAG_TEXT];
      if (event == ESP_BT_GAP_CFM_REQ_EVT) {
        uint8_t* b = param->cfm_req.bda;
        Serial.printf("[BT] 配對比對數字 %06lu（自動同意）\n", (unsigned long)param->cfm_req.num_val);
        snprintf(d, sizeof(d), "P %02X%02X%02X cfm %06lu", b[3], b[4], b[5], (unsigned long)param->cfm_req.num_val);
      } else if (event == ESP_BT_GAP_KEY_NOTIF_EVT) {
        uint8_t* b = param->key_notif.bda;
        Serial.printf("[BT] 配對顯示 passkey %06lu\n", (unsigned long)param->key_notif.passkey);
        snprintf(d, sizeof(d), "P %02X%02X%02X notif %06lu", b[3], b[4], b[5], (unsigned long)param->key_notif.passkey);
      } else {
        uint8_t* b = param->key_req.bda;
        Serial.println("[BT] 對方要求輸入 passkey（無法輸入）");
        snprintf(d, sizeof(d), "P %02X%02X%02X keyreq", b[3], b[4], b[5]);
      }
      diag_post(d);
    }
    BluetoothA2DPSource::app_gap_callback(event, param);
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
static volatile bool g_page_failed_evt = false;

// ---- 已配對清單 / 模式 / 按鈕與 LED ----
static const char* TARGET_NAMES[] = {"BMW 04900", "soundcore"};
// 名稱拿不到的目標改用位址認（位址取自 Pi Zero carpuck2 已配對清單）
struct TargetAddr { uint8_t a[6]; const char* label; };
static const TargetAddr TARGET_ADDRS[] = {
  {{0xB8, 0x24, 0x10, 0x12, 0x78, 0x50}, "BMW 04900"},
};
static KnownDevices g_known;
static bool g_pairing_mode = false;

// 掃到的位址是否為目標；配對模式下已配對過的不算（跟 ssid_match_cb 同規則）
const char* addr_target_label(const uint8_t* bda) {
  for (const TargetAddr& t : TARGET_ADDRS) {
    if (memcmp(t.a, bda, 6) != 0) continue;
    if (g_pairing_mode && known_find(g_known, bda) >= 0) {
      Serial.printf("[BT] 配對模式：略過已配對 '%s'\n", t.label);
      return nullptr;
    }
    return t.label;
  }
  return nullptr;
}
static const int BOOT_BTN_GPIO = 0;
static const int LED_PIN = 2;
static const uint32_t BOOT_LONG_MS = 3000;
static const uint32_t PAIRING_TIMEOUT_MS = 180000;  // 配對模式 3 分鐘沒連上任何裝置 → 退回重連已知裝置
static const uint32_t LED_FAST_MS = 125;
static const uint32_t LED_SLOW_MS = 500;

static void print_bda(const uint8_t a[6]) {
  Serial.printf("%02X:%02X:%02X:%02X:%02X:%02X", a[0], a[1], a[2], a[3], a[4], a[5]);
}

static void known_load() {
  uint8_t buf[1 + 6 * KNOWN_MAX];
  nvs_handle_t h;
  if (nvs_open("bt_bridge", NVS_READONLY, &h) == ESP_OK) {
    size_t len = sizeof(buf);
    if (nvs_get_blob(h, "known", buf, &len) == ESP_OK) {
      known_deserialize(g_known, buf, len);
    }
    nvs_close(h);
  }
  Serial.printf("[BT] 已配對裝置 %d 台\n", (int)g_known.count);
}

static void known_save() {
  uint8_t buf[1 + 6 * KNOWN_MAX];
  size_t len = known_serialize(g_known, buf, sizeof(buf));
  nvs_handle_t h;
  if (nvs_open("bt_bridge", NVS_READWRITE, &h) != ESP_OK) {
    Serial.println("[BT] 已配對清單寫入失敗（nvs_open）");
    return;
  }
  esp_err_t err = nvs_set_blob(h, "known", buf, len);
  if (err == ESP_OK) err = nvs_commit(h);
  nvs_close(h);
  if (err != ESP_OK) Serial.printf("[BT] 已配對清單寫入失敗 err=%d\n", (int)err);
}

// ---- 配對診斷紀錄 NVS（namespace "bt_bridge" / key "diag"）----
static void diag_load() {
  uint8_t buf[1 + DIAG_TEXT * DIAG_MAX];
  nvs_handle_t h;
  if (nvs_open("bt_bridge", NVS_READONLY, &h) == ESP_OK) {
    size_t len = sizeof(buf);
    if (nvs_get_blob(h, "diag", buf, &len) == ESP_OK) {
      diag_deserialize(g_diag, buf, len);
    }
    nvs_close(h);
  }
  Serial.printf("[DIAG] 上次診斷紀錄 %d 條\n", (int)g_diag.count);
  for (int i = 0; i < g_diag.count; i++) {
    char t[DIAG_TEXT];
    diag_get(g_diag, i, t);
    Serial.printf("[DIAG] %02d %s\n", i, t);
  }
}

static void diag_store() {
  uint8_t buf[1 + DIAG_TEXT * DIAG_MAX];
  size_t len = diag_serialize(g_diag, buf, sizeof(buf));
  nvs_handle_t h;
  if (nvs_open("bt_bridge", NVS_READWRITE, &h) != ESP_OK) {
    Serial.println("[DIAG] 寫入失敗（nvs_open）");
    return;
  }
  esp_err_t err = nvs_set_blob(h, "diag", buf, len);
  if (err == ESP_OK) err = nvs_commit(h);
  nvs_close(h);
  if (err != ESP_OK) Serial.printf("[DIAG] 寫入失敗 err=%d\n", (int)err);
}

// loop() 內取出佇列並寫入紀錄；BT callback 不直接呼叫這裡。
static void diag_drain() {
  char q[DIAG_Q][DIAG_TEXT];
  uint8_t n;
  uint32_t drop;
  portENTER_CRITICAL(&g_diag_mux);
  n = g_diag_q_n;
  memcpy(q, g_diag_q, sizeof(q));
  g_diag_q_n = 0;
  drop = g_diag_q_drop;
  g_diag_q_drop = 0;
  portEXIT_CRITICAL(&g_diag_mux);
  if (drop) Serial.printf("[DIAG] 佇列滿，丟棄 %lu 條\n", (unsigned long)drop);
  if (n == 0) return;
  for (int i = 0; i < n; i++) diag_add(g_diag, q[i]);
  diag_store();
}

static void diag_loop_add(const char* text) {
  diag_add(g_diag, text);
  diag_store();
}

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
  bool named = false;
  for (const char* name : TARGET_NAMES) {
    if (contains_ignore_case(ssid, name)) named = true;
  }
  if (!named) return false;
  if (g_pairing_mode && known_find(g_known, address) >= 0) {
    Serial.printf("[BT] 配對模式：略過已配對 '%s'\n", ssid);
    return false;
  }
  return true;
}

// ---- 連線狀態 callback（只設旗標，reconnect 由 loop() 處理）----
void connection_state_cb(esp_a2d_connection_state_t state, void* obj) {
  static esp_a2d_connection_state_t prev = ESP_A2D_CONNECTION_STATE_DISCONNECTED;
  Serial.printf("[BT] 連線狀態 -> %s\n", a2dp_source.to_str(state));
  if (state == ESP_A2D_CONNECTION_STATE_CONNECTED) {
    g_connected_evt = true;
  }
  if (state == ESP_A2D_CONNECTION_STATE_DISCONNECTED && prev == ESP_A2D_CONNECTION_STATE_CONNECTING) {
    g_page_failed_evt = true;
  }
  prev = state;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[BT] bt_bridge STEP3 boot");
  pinMode(BOOT_BTN_GPIO, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // I2S 失敗就不起 reader task，只讓 BT 照常跑（靜音），避免空讀迴圈吃光 CPU
  if (i2s_rx_init()) {
    xTaskCreatePinnedToCore(i2s_reader_task, "i2s_rd", 4096, NULL, 10, NULL, 1);
  }

  a2dp_source.set_data_callback_in_frames(bridge_data_cb);
  a2dp_source.set_ssid_callback(ssid_match_cb);
  a2dp_source.set_on_connection_state_changed(connection_state_cb);
  a2dp_source.set_auto_reconnect(true);
  // 開 SSP：IO 能力宣告為 DisplayYesNo（ESP_BT_IO_CAP_IO），比對數字請求函式庫自動同意。
  // 預設 false 不設 IO 能力，BMW 回 AUTH_CMPL stat=9（AUTH_FAILURE，10/7 [DIAG] 實測）。
  a2dp_source.set_ssp_enabled(true);
  a2dp_source.set_volume(127);
  a2dp_source.start();
  // 已配對清單在 start() 之後才讀：start() 內部已 init NVS，我們這邊再 init 會讓函式庫 ESP_ERROR_CHECK 失敗
  known_load();
  diag_load();
  g_pairing_mode = a2dp_source.pairing_mode();
  Serial.printf("[BT] 模式：%s\n", g_pairing_mode ? "配對(掃描)" : "重連已知裝置");
}

void loop() {
  static uint32_t last_ms = 0;
  static uint32_t last_frames = 0;
  static uint32_t led_ms = 0;
  static bool led_on = false;
  static bool boot_was_down = false;
  static uint32_t boot_down_ms = 0;

  diag_drain();

  if (g_connected_evt) {
    g_connected_evt = false;
    if (a2dp_source.arm_reconnect()) {
      Serial.println("[BT] 首次配對：補開自動重連");
    }
    uint8_t cur[6];
    a2dp_source.get_last(cur);
    if (!addr_is_zero(cur)) {
      known_touch(g_known, cur);
      known_save();
      Serial.print("[BT] 已記住裝置 ");
      print_bda(cur);
      Serial.printf("（共 %d 台）\n", (int)g_known.count);
    }
    // C/F 只在配對模式記：重連模式下目標沒開機會每 ~10s 失敗一次，整晚寫 NVS 白耗壽命
    if (g_pairing_mode) {
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "C %02X%02X%02X", cur[3], cur[4], cur[5]);
      diag_loop_add(d);
    }
    g_pairing_mode = false;
  }

  if (g_page_failed_evt) {
    g_page_failed_evt = false;
    if (g_pairing_mode) {
      uint8_t lc[6];
      a2dp_source.get_last(lc);
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "F %02X%02X%02X", lc[3], lc[4], lc[5]);
      diag_loop_add(d);
    }
    // >=1 而非 >=2：上次連線對象可能是「配對中途斷電、從沒連成功」的裝置（不在清單裡，
    // 例如 10/7 的 BMW），這時清單只有 1 台也要切過去，否則永遠 page 一台不存在的車機
    if (!a2dp_source.pairing_mode() && g_known.count >= 1) {
      uint8_t cur[6], nxt[6];
      a2dp_source.get_last(cur);
      if (known_next_after_fail(g_known, cur, nxt)) {
        a2dp_source.retarget(nxt);
        Serial.print("[BT] 連線失敗，下一台 ");
        print_bda(nxt);
        Serial.println();
      }
    }
  }

  uint32_t now = millis();

  // 配對模式逾時：否則配對失敗（例如 BMW 沒成功）就永遠卡在配對模式，連已配對的 Soundcore 都不回頭連。
  // 把最近連過的裝置寫回函式庫的 src_bda 再重開 → 開機即進入重連模式，照常在已知裝置間輪替。
  if (g_pairing_mode && !a2dp_source.is_connected() && g_known.count > 0 && now >= PAIRING_TIMEOUT_MS) {
    Serial.println("[BT] 配對模式逾時（3 分鐘未連上），退回重連已知裝置");
    nvs_handle_t h;
    if (nvs_open("connected_bda", NVS_READWRITE, &h) == ESP_OK) {
      nvs_set_blob(h, "src_bda", g_known.addr[0], 6);
      nvs_commit(h);
      nvs_close(h);
    }
    delay(200);
    ESP.restart();
  }

  // BOOT 長按 3 秒：清除上次裝置（src_bda）、重開進入配對模式；已知清單保留
  bool boot_down = digitalRead(BOOT_BTN_GPIO) == LOW;
  if (boot_down && !boot_was_down) {
    boot_down_ms = now;
  }
  boot_was_down = boot_down;
  if (boot_down && now - boot_down_ms >= BOOT_LONG_MS) {
    Serial.println("[BT] 長按 BOOT：清除上次裝置，重開進入配對模式");
    g_diag = DiagLog();  // 紀錄只反映最近一次配對嘗試
    diag_store();
    nvs_handle_t h;
    if (nvs_open("connected_bda", NVS_READWRITE, &h) == ESP_OK) {
      esp_err_t err = nvs_erase_key(h, "src_bda");
      if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;
      if (err == ESP_OK) nvs_commit(h);
      nvs_close(h);
    }
    delay(200);
    ESP.restart();
  }

  // LED：恆亮=已連線；配對中未連線=快閃；重連中未連線=慢閃
  if (a2dp_source.is_connected()) {
    if (!led_on) {
      led_on = true;
      digitalWrite(LED_PIN, HIGH);
    }
  } else {
    uint32_t period = g_pairing_mode ? LED_FAST_MS : LED_SLOW_MS;
    if (now - led_ms >= period) {
      led_ms = now;
      led_on = !led_on;
      digitalWrite(LED_PIN, led_on ? HIGH : LOW);
    }
  }

  if (now - last_ms >= 5000) {
    uint32_t cur = g_frames_in;
    uint32_t in_rate = (cur - last_frames) / 5;
    last_frames = cur;
    last_ms = now;
    Serial.printf("[BR] conn=%d in_rate=%lu fill=%lu prime=%d under=%lu over=%lu drop=%lu dup=%lu known=%d mode=%s\n",
                  a2dp_source.is_connected() ? 1 : 0,
                  (unsigned long)in_rate,
                  (unsigned long)g_ring.fill(),
                  g_out_state.priming ? 1 : 0,
                  (unsigned long)g_out_state.underruns,
                  (unsigned long)g_ring.overruns,
                  (unsigned long)g_out_state.drops,
                  (unsigned long)g_out_state.dups,
                  (int)g_known.count,
                  g_pairing_mode ? "pair" : "recon");
  }
  delay(25);
}
