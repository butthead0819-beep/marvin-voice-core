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
 *       目標裝置：配對模式下自動認任何車機/藍牙喇叭（COD 主類別 Audio/Video＋Rendering/Audio 服務，
 *       見 cod_filter.h；手機、電腦不收）；名稱 "BMW 04900"/"soundcore" 與 BMW 位址比對保留為後備。
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
 *   arduino-cli compile -b esp32:esp32:esp32 --build-property "compiler.c.elf.extra_flags=-Wl,--wrap=AVRC_BldResponse -Wl,--wrap=BTA_AvMetaRsp" firmware/bt_bridge
 *   arduino-cli upload  -b esp32:esp32:esp32 -p /dev/cu.usbserial-0001 firmware/bt_bridge
 *
 *   ⚠️ 少了 --wrap 旗標會連結失敗（__real_AVRC_BldResponse/__real_BTA_AvMetaRsp 找
 *   不到）——這是刻意的（見 avrc_meta_wrap.cpp），防止忘記加旗標卻燒進去一個靜默
 *   不顯示曲名的版本。
 *
 * AVRCP 曲名診斷 DIAG 代碼（車機沒顯示曲名時用來分辨卡在哪一段）：
 *   "M ok"   = pollNowPlayingFromS3 第一次從 S3 解析出曲目資訊成功（開機後僅記一次）。
 *   "G elem"/"G stat" = 車機來問 GetElementAttributes/GetPlayStatus 且當下有曲目資訊可答。
 *   "G nov e"/"G nov s" = 車機來問 GetElementAttributes/GetPlayStatus，但當下沒有真曲目
 *                         資訊，回了佔位資料（標題"Marvin"／song_len,song_pos=0xFFFFFFFF）
 *                         ——代表「車機有問，WROOM 沒資料」，不是「車機根本沒來問」。
 *   "R <id>" = 車機第一次對某個 event_id 送 AVRCP RegisterNotification（0–15，每個 id
 *              開機後只記一次），確認車機有沒有訂閱 TRACK_CHANGE(2)/PLAY_STATUS_CHANGE(1)
 *              等通知。
 *   "Rf <feat_mask hex> <ct_feat_flag hex>" = 車機當 CT 的 AVRC 能力（SDP 探出來的），開機
 *              後只記一次。
 */

#include "BluetoothA2DPSource.h"
#include <driver/i2s_std.h>
#include "bridge_dsp.h"
#include "known_devices.h"
#include "diag_log.h"
#include "diag_summary.h"
#include "cod_filter.h"
#include "now_playing.h"
#include "avrc_meta_wrap.h"
#include <nvs.h>

// ---- 配對診斷紀錄的 thread 安全佇列 ----
// BT callback 只把字串丟進這 4 格（portMUX 保護），loop() 取出後寫進 g_diag 並存 NVS。
static const int DIAG_Q = 4;
static char g_diag_q[DIAG_Q][DIAG_TEXT];
static uint8_t g_diag_q_n = 0;
static uint32_t g_diag_q_drop = 0;
static portMUX_TYPE g_diag_mux = portMUX_INITIALIZER_UNLOCKED;
static DiagLog g_diag;
static A2DPNoVolumeControl g_no_vol;

// ---- 每次開機統計摘要（見 diag_summary.h）：固定一筆，隨時覆寫，不會被 g_diag 環形
// 擠掉。BT/BTC task 只能改計數欄位並設 g_sum_dirty，不准寫 NVS、不准 print；
// loop() 負責存。----
DiagSummary g_sum;
volatile bool g_sum_dirty = false;

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

// 配對模式整輪掃描挑 RSSI 最強的 COD 候選（STARTED 時重置，STOPPED 時才真正 adopt）
static bool g_cand_valid = false;
static uint8_t g_cand_bda[6];
static int g_cand_rssi = -129;
static char g_cand_name[ESP_BT_GAP_MAX_BDNAME_LEN + 1];

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
bool pairing_accepts_cod(const uint8_t* bda, uint32_t cod);

// 重連模式真正連上的位址（對方主動連入時，函式庫的 peer_bd_addr/last_connection
// 不一定會先被我方設好，靠 CONNECTED 事件的 remote_bda 才準）。
static uint8_t g_conn_bda[6];
static volatile bool g_conn_bda_valid = false;
static volatile char g_conn_from = '?';  // 'C'=我方連線中收到 / 'U'=對方主動連入 / 'O'=其他狀態

// ---- 車機顯示曲名：目前播放狀態，只有 loop()（Arduino loopTask）讀寫。BT app task 的
// av_hdl_avrc_tg_evt 要讀時改走 avrc_meta_get() 加鎖快照，不碰這份。----
static NowPlaying g_np;

// ---- AVRCP TG 通知登記狀態：av_hdl_avrc_tg_evt（BT app task）跟 loop()（Arduino
// loopTask）兩邊只做單純設/清，不做複合讀改寫，volatile bool 足夠，不需要鎖。----
static volatile bool g_rn_track_registered = false;
static volatile bool g_rn_status_registered = false;
static volatile bool g_rn_pos_registered = false;
static volatile bool g_rn_track_due = false;     // 換歌，待送 TRACK_CHANGE CHANGED
static volatile bool g_rn_status_due = false;    // 播放狀態變了，待送 PLAY_STATUS_CHANGE CHANGED
static volatile bool g_rn_pos_force_due = false; // 換歌時位置也要立刻跟著送一次
static volatile uint32_t g_rn_pos_interval_s = 1;
static uint32_t g_rn_pos_last_sent_ms = 0;

// ---- 回報給車機的播放狀態＝車機最後一次下的方向盤指令（WROOM 只要 A2DP 連著就一直送
// 音訊，沒有自己的播放/暫停狀態）。on_wheel_key（BT app task）寫，av_hdl_avrc_tg_evt
// （BT app task）跟 sendDueRnNotifications（loop()）讀，單一 byte 不需要鎖。----
volatile uint8_t g_reported_playback = ESP_AVRC_PLAYBACK_PLAYING;

// ---- 方向盤按鍵：on_wheel_key（BT app task）推進、loop() 取出——單生產者單消費者
// ring，head 只被 producer 動、tail 只被 consumer 動，不需要鎖。滿了就丟（這階段只是
// log 通道，不是控制通道，丟了不影響播放）。----
struct WheelEvent { uint8_t key; bool released; };
static const int WHEEL_Q = 16;
static WheelEvent g_wheel_q[WHEEL_Q];
static volatile uint8_t g_wheel_q_head = 0;
static volatile uint8_t g_wheel_q_tail = 0;

// ---- 首次配對補開重連 ----
// 函式庫在 NVS 沒有上次位址時會整段關掉自動重連，且之後不再打開，這裡補開。
class BridgeSource : public BluetoothA2DPSource {
 public:
  BridgeSource() { discoverability = ESP_BT_NON_DISCOVERABLE; }

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
  // esp-idf #15913：已配對 sink（車機/喇叭）開機會主動連「上次的裝置」，忙著主動連時不回應
  // ESP32 的 page，導致 ESP32 連 5–10 次才成功。函式庫預設重連模式也設不可連入（寫死 false），
  // 這裡改成：配對模式維持不可連入（避免路人誤連），重連模式開放可連入但仍不可被發現
  // （discoverability 建構子已設 NON_DISCOVERABLE）。
  void set_scan_mode_connectable_default() override {
    set_scan_mode_connectable(!pairing_mode());
  }

  // 函式庫在 APP_AV_STATE_UNCONNECTED 狀態收到 CONNECTED 事件會被忽略（unconnected_hdlr
  // 只處理 DISCONNECTED），對方主動連入時我方仍是 UNCONNECTED → 狀態機不認、永遠卡住。
  // 這裡在呼叫基底前，於 UNCONNECTED 狀態下把 CONNECTED 事件先轉成 CONNECTING，
  // 讓基底的 connecting_hdlr 照正常流程轉成 CONNECTED。
  void bt_app_av_sm_hdlr(uint16_t event, void* param) override {
    if (event == ESP_A2D_CONNECTION_STATE_EVT && param) {
      auto* a2d = (esp_a2d_cb_param_t*)param;
      if (a2d->conn_stat.state == ESP_A2D_CONNECTION_STATE_CONNECTED) {
        memcpy(g_conn_bda, a2d->conn_stat.remote_bda, 6);
        g_conn_bda_valid = true;
        g_conn_from = (s_a2d_state == APP_AV_STATE_CONNECTING)
                          ? 'C'
                          : (s_a2d_state == APP_AV_STATE_UNCONNECTED ? 'U' : 'O');
        if (!pairing_mode() && s_a2d_state == APP_AV_STATE_UNCONNECTED) {
          s_a2d_state = APP_AV_STATE_CONNECTING;
        }
        set_last_connection(a2d->conn_stat.remote_bda);
        memcpy(peer_bd_addr, a2d->conn_stat.remote_bda, 6);
      }
    }
    BluetoothA2DPSource::bt_app_av_sm_hdlr(event, param);
  }

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

    // 配對模式：任何未配對過的車機/藍牙喇叭都收（不靠名稱/位址，Lexus 等新車免改韌體）。
    // 不馬上 adopt：整輪掃描收完才挑 RSSI 最強的一個（見 app_gap_callback 的 DISC STOPPED）。
    if (pairing_accepts_cod(bda, cod)) {
      const char* label = name[0] != '\0' ? name : "音訊裝置";
      if (cod_candidate_better(g_cand_valid, (int)rssi, g_cand_rssi)) {
        memcpy(g_cand_bda, bda, 6);
        g_cand_rssi = (int)rssi;
        strncpy(g_cand_name, label, ESP_BT_GAP_MAX_BDNAME_LEN);
        g_cand_name[ESP_BT_GAP_MAX_BDNAME_LEN] = '\0';
        g_cand_valid = true;
      }
      Serial.printf("[SCAN] 配對模式候選 '%s' %s rssi=%d\n", label, bs, (int)rssi);
      return;
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
    if (event == ESP_BT_GAP_DISC_STATE_CHANGED_EVT) {
      if (param->disc_st_chg.state == ESP_BT_GAP_DISCOVERY_STARTED) {
        g_cand_valid = false;
        g_cand_rssi = -129;
      } else if (param->disc_st_chg.state == ESP_BT_GAP_DISCOVERY_STOPPED && g_cand_valid &&
                 s_a2d_state != APP_AV_STATE_DISCOVERED) {
        char d[DIAG_TEXT];
        snprintf(d, sizeof(d), "V %02X%02X%02X r%d '%s'", g_cand_bda[3], g_cand_bda[4], g_cand_bda[5], g_cand_rssi,
                 g_cand_name);
        diag_post(d);
        Serial.printf("[SCAN] 配對模式選中 '%s' rssi=%d\n", g_cand_name, g_cand_rssi);
        adopt_target(g_cand_bda, g_cand_name);
        g_cand_valid = false;
      }
    }
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

 public:
  // 車機送來的 AVRCP 絕對音量通知；只記錄驗證用，不衰減 PCM（見 g_no_vol）。
  void set_volume(uint8_t volume) override {
    Serial.printf("[BT] 音量 -> %d\n", (int)volume);
    if (volume != s_last_vol_logged && s_vol_diag_n < 4) {
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "Q v%d", (int)volume);
      diag_post(d);
      s_last_vol_logged = volume;
      s_vol_diag_n++;
    }
    BluetoothA2DPSource::set_volume(volume);
  }

  // 車機訂閱 TRACK_CHANGE/PLAY_STATUS_CHANGE/PLAY_POS_CHANGED 三種通知——只處理這三種，
  // 其他事件（含 VOLUME_CHANGE 的註冊、PASSTHROUGH_CMD 等）一律交給父類別維持原行為。
  void av_hdl_avrc_tg_evt(uint16_t event, void* p_param) override {
    if (event == ESP_AVRC_TG_REGISTER_NOTIFICATION_EVT) {
      auto* rc = (esp_avrc_tg_cb_param_t*)p_param;
      if (rc->reg_ntf.event_id < 16 && !(s_rn_event_seen & (1u << rc->reg_ntf.event_id))) {
        s_rn_event_seen |= (1u << rc->reg_ntf.event_id);
        char d[DIAG_TEXT];
        snprintf(d, sizeof(d), "R %d", (int)rc->reg_ntf.event_id);
        diag_post(d);
      }
      if (rc->reg_ntf.event_id < 16) {
        g_sum.rn_mask |= (uint16_t)(1u << rc->reg_ntf.event_id);
        g_sum_dirty = true;
      }
      NowPlaying np;
      avrc_meta_get(np);
      esp_avrc_rn_param_t param;
      memset(&param, 0, sizeof(param));
      switch (rc->reg_ntf.event_id) {
        case ESP_AVRC_RN_TRACK_CHANGE:
          g_rn_track_registered = true;
          np_fill_elm_id(np, param.elm_id);
          esp_avrc_tg_send_rn_rsp(ESP_AVRC_RN_TRACK_CHANGE, ESP_AVRC_RN_RSP_INTERIM, &param);
          return;
        case ESP_AVRC_RN_PLAY_STATUS_CHANGE:
          g_rn_status_registered = true;
          param.playback = (esp_avrc_playback_stat_t)g_reported_playback;
          esp_avrc_tg_send_rn_rsp(ESP_AVRC_RN_PLAY_STATUS_CHANGE, ESP_AVRC_RN_RSP_INTERIM, &param);
          return;
        case ESP_AVRC_RN_PLAY_POS_CHANGED:
          g_rn_pos_registered = true;
          g_rn_pos_interval_s = rc->reg_ntf.event_parameter > 0 ? rc->reg_ntf.event_parameter : 1;
          param.play_pos = np_position_now(np, millis());
          esp_avrc_tg_send_rn_rsp(ESP_AVRC_RN_PLAY_POS_CHANGED, ESP_AVRC_RN_RSP_INTERIM, &param);
          return;
        default:
          break;  // 其他事件（含 VOLUME_CHANGE）落到下面交給父類別
      }
    } else if (event == ESP_AVRC_TG_CONNECTION_STATE_EVT) {
      auto* rc = (esp_avrc_tg_cb_param_t*)p_param;
      if (!rc->conn_stat.connected) {
        g_rn_track_registered = false;
        g_rn_status_registered = false;
        g_rn_pos_registered = false;
      }
    } else if (event == ESP_AVRC_TG_REMOTE_FEATURES_EVT) {
      auto* rc = (esp_avrc_tg_cb_param_t*)p_param;
      if (!s_rmt_feats_logged) {
        s_rmt_feats_logged = true;
        char d[DIAG_TEXT];
        snprintf(d, sizeof(d), "Rf %lx %x", (unsigned long)rc->rmt_feats.feat_mask,
                 (unsigned)rc->rmt_feats.ct_feat_flag);
        diag_post(d);
      }
      g_sum.rf_feat = (uint32_t)rc->rmt_feats.feat_mask;
      g_sum.rf_ct = (uint16_t)rc->rmt_feats.ct_feat_flag;
      g_sum_dirty = true;
    }
    BluetoothA2DPSource::av_hdl_avrc_tg_evt(event, p_param);
  }

 private:
  static int s_last_vol_logged;
  static int s_vol_diag_n;
  static uint16_t s_rn_event_seen;
  static bool s_rmt_feats_logged;
};

int BridgeSource::s_last_vol_logged = -1;
int BridgeSource::s_vol_diag_n = 0;
uint16_t BridgeSource::s_rn_event_seen = 0;
bool BridgeSource::s_rmt_feats_logged = false;

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
static volatile bool g_dropped_evt = false;

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

// 配對模式下依裝置類型收車機/喇叭；已配對過的不收（配對模式只為了加新裝置）
bool pairing_accepts_cod(const uint8_t* bda, uint32_t cod) {
  return g_pairing_mode && cod_is_audio_sink(cod) && known_find(g_known, bda) < 0;
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

// 量產/使用中清掉誤配的裝置（例：10/13 配對模式把 AeroClip 2 耳機當喇叭配進來）。
// 刪 bond 是因為重連模式接受對方主動連入，只刪清單裡的位址耳機還是會自己連回來。
static void handleSerialCommand(const char* line) {
  static const char PREFIX[] = "forget ";
  static const size_t PREFIX_LEN = sizeof(PREFIX) - 1;
  if (strncmp(line, PREFIX, PREFIX_LEN) == 0) {
    const char* addr_str = line + PREFIX_LEN;
    uint8_t bda[6];
    if (!parse_bda(addr_str, strlen(addr_str), bda)) {
      Serial.println("[CMD] 用法：forget XX:XX:XX:XX:XX:XX");
      return;
    }

    esp_err_t be = esp_bt_gap_remove_bond_device(bda);
    bool kr = known_remove(g_known, bda);
    if (kr) known_save();

    bool erased = false;
    nvs_handle_t h;
    if (nvs_open("connected_bda", NVS_READWRITE, &h) == ESP_OK) {
      uint8_t stored[6];
      size_t slen = sizeof(stored);
      if (nvs_get_blob(h, "src_bda", stored, &slen) == ESP_OK && slen == 6 && memcmp(stored, bda, 6) == 0) {
        esp_err_t err = nvs_erase_key(h, "src_bda");
        if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;
        if (err == ESP_OK) {
          nvs_commit(h);
          erased = true;
        }
      }
      nvs_close(h);
    }

    Serial.print("[CMD] forget ");
    print_bda(bda);
    Serial.print("  ");
    Serial.printf("bond=%d known=%d src_bda=%d 剩 %d 台\n", (int)be, kr ? 1 : 0, erased ? 1 : 0, (int)g_known.count);
    for (int i = 0; i < g_known.count; i++) {
      Serial.print("  ");
      print_bda(g_known.addr[i]);
      Serial.println();
    }

    // 正連著這台就重開，讓它重新跑重連流程。不能用 g_conn_bda_valid：那個旗標只在
    // 剛連上那一圈 loop 有效，之後就清掉了。
    if (a2dp_source.is_connected() && memcmp(*a2dp_source.get_last_peer_address(), bda, 6) == 0) {
      delay(200);
      ESP.restart();
    }
    return;
  }
  if (strcmp(line, "sum") == 0) {
    char out[160];
    Serial.printf("[SUM] 本次 %s\n", ds_format(g_sum, out, sizeof(out)));
    nvs_handle_t hc;
    if (nvs_open("bt_bridge", NVS_READONLY, &hc) == ESP_OK) {
      uint8_t cbuf[sizeof(DiagSummary)];
      size_t clen = sizeof(cbuf);
      if (nvs_get_blob(hc, "sumc", cbuf, &clen) == ESP_OK && clen == sizeof(DiagSummary)) {
        DiagSummary lastConn;
        memcpy(&lastConn, cbuf, sizeof(lastConn));
        char cline[160];
        Serial.printf("[SUM] 最近連上車機那次 %s\n", ds_format(lastConn, cline, sizeof(cline)));
      }
      nvs_close(hc);
    }
    return;
  }
  Serial.println("[CMD] 未知指令（可用：forget XX:XX:XX:XX:XX:XX、sum）");
}

// 非阻塞讀 USB Serial 湊行；上限 64 bytes，超長丟整行。
static void pollSerialCommand() {
  static char buf[64];
  static size_t idx = 0;
  static bool overflow = false;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (!overflow && idx > 0) {
        buf[idx] = '\0';
        handleSerialCommand(buf);
      }
      idx = 0;
      overflow = false;
      continue;
    }
    if (idx < sizeof(buf) - 1) {
      buf[idx++] = c;
    } else {
      overflow = true;
    }
  }
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

// ---- 每次開機統計摘要 NVS（namespace "bt_bridge" / key "sum0" 本次、"sum1" 上次、
// "sumc" 最近一次「有 A2DP 連上」那次開機的摘要）----
//
// 2026-10-14：每次開機都會把 sum0 搬成 sum1，但下車後在家插 USB、序列埠一開又會重開
// 一次機——那趟車上session 的 sum0/sum1 就被這次在家開機推掉了。sumc 只在 g_sum.peer
// 非全0（這次開機真的連上過車機）時才覆寫，所以沒連過線的開機（比如在家插 USB）不會
// 蓋掉它，可以隨時回去看「最近一次連上車機」那趟的摘要。
static void sum_load() {
  uint8_t buf[sizeof(DiagSummary)];
  nvs_handle_t h;
  if (nvs_open("bt_bridge", NVS_READONLY, &h) == ESP_OK) {
    size_t len = sizeof(buf);
    if (nvs_get_blob(h, "sum0", buf, &len) == ESP_OK && len == sizeof(DiagSummary)) {
      DiagSummary prev;
      memcpy(&prev, buf, sizeof(prev));
      nvs_close(h);
      nvs_handle_t hw;
      if (nvs_open("bt_bridge", NVS_READWRITE, &hw) == ESP_OK) {
        nvs_set_blob(hw, "sum1", &prev, sizeof(prev));
        nvs_commit(hw);
        nvs_close(hw);
      }
      char line[160];
      Serial.printf("[SUM] 上次開機 %s\n", ds_format(prev, line, sizeof(line)));
    } else {
      nvs_close(h);
      Serial.println("[SUM] 無上次開機紀錄");
    }
  } else {
    Serial.println("[SUM] 無上次開機紀錄");
  }

  nvs_handle_t hc;
  if (nvs_open("bt_bridge", NVS_READONLY, &hc) == ESP_OK) {
    uint8_t cbuf[sizeof(DiagSummary)];
    size_t clen = sizeof(cbuf);
    if (nvs_get_blob(hc, "sumc", cbuf, &clen) == ESP_OK && clen == sizeof(DiagSummary)) {
      DiagSummary lastConn;
      memcpy(&lastConn, cbuf, sizeof(lastConn));
      char cline[160];
      Serial.printf("[SUM] 最近連上車機那次 %s\n", ds_format(lastConn, cline, sizeof(cline)));
    }
    nvs_close(hc);
  }
}

static uint32_t g_sum_last_store_ms = 0;
static void sum_store() {
  nvs_handle_t h;
  if (nvs_open("bt_bridge", NVS_READWRITE, &h) != ESP_OK) {
    Serial.println("[SUM] 寫入失敗（nvs_open）");
    return;
  }
  esp_err_t err = nvs_set_blob(h, "sum0", &g_sum, sizeof(g_sum));
  bool hasPeer = (g_sum.peer[0] || g_sum.peer[1] || g_sum.peer[2]);
  if (err == ESP_OK && hasPeer) {
    err = nvs_set_blob(h, "sumc", &g_sum, sizeof(g_sum));
  }
  if (err == ESP_OK) err = nvs_commit(h);
  nvs_close(h);
  if (err != ESP_OK) Serial.printf("[SUM] 寫入失敗 err=%d\n", (int)err);
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
  for (int i = 0; i < n; i++) diag_add_ts(g_diag, millis() / 1000, q[i]);
  diag_store();
}

static void diag_loop_add(const char* text) {
  diag_add_ts(g_diag, millis() / 1000, text);
  diag_store();
}

// 重連模式每次開機最多記 8 筆（避免目標不在時每 ~10s 失敗一次整晚寫 NVS）。
static const uint8_t RECON_DIAG_MAX = 8;
static uint8_t g_recon_diag_used = 0;
static void recon_diag_add(const char* text) {
  if (diag_budget_take(g_recon_diag_used, RECON_DIAG_MAX)) diag_loop_add(text);
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

// ---- 方向盤按鍵：車機 AVRCP passthrough → 函式庫 callback（BT app task）→ 這裡只推
// 進 ring，不做任何播放控制（第二刀才接），交給 loop() 的 drainWheelKeys() 轉送 S3。----
// AVRCP TRACK_CHANGE 用的 elm_id：np.valid 且 track_id 非全 0 才回真 track_id，否則回
// 「尚未選曲」的全 FF（NowPlaying 預設建構值 track_id 全 0，不能直接當真 track_id 送出）。
static void np_fill_elm_id(const NowPlaying& np, uint8_t out[8]) {
  static const uint8_t kZero8[8] = {0};
  if (np.valid && memcmp(np.track_id, kZero8, 8) != 0) {
    memcpy(out, np.track_id, 8);
  } else {
    memset(out, 0xFF, 8);
  }
}

static void on_wheel_key(uint8_t key, bool isReleased) {
  if (!isReleased) {
    ds_add_key(g_sum, key);
    g_sum_dirty = true;
    if (key == ESP_AVRC_PT_CMD_PLAY) {
      g_reported_playback = ESP_AVRC_PLAYBACK_PLAYING;
      g_rn_status_due = true;
    } else if (key == ESP_AVRC_PT_CMD_PAUSE) {
      g_reported_playback = ESP_AVRC_PLAYBACK_PAUSED;
      g_rn_status_due = true;
    }
  }
  uint8_t next = (uint8_t)((g_wheel_q_head + 1) % WHEEL_Q);
  if (next == g_wheel_q_tail) return;  // 滿了，丟（這階段只是 log 通道）
  g_wheel_q[g_wheel_q_head] = {key, isReleased};
  g_wheel_q_head = next;
}

static const uint8_t WHEEL_DIAG_MAX = 12;
static uint8_t g_wheel_diag_used = 0;

// loop() 呼叫：把 ring 裡的按鍵事件轉送給 S3（Serial2），印 log，記 DIAG（每次開機上限
// WHEEL_DIAG_MAX 筆，避免方向盤被狂轉時整晚洗 NVS）。
static void drainWheelKeys() {
  while (g_wheel_q_tail != g_wheel_q_head) {
    WheelEvent ev = g_wheel_q[g_wheel_q_tail];
    g_wheel_q_tail = (uint8_t)((g_wheel_q_tail + 1) % WHEEL_Q);
    Serial2.printf("K|%02x|%c\n", ev.key, ev.released ? 'r' : 'p');
    Serial.printf("[Wheel] key=0x%02x %s\n", ev.key, ev.released ? "release" : "press");
    if (diag_budget_take(g_wheel_diag_used, WHEEL_DIAG_MAX)) {
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "K %02x %c", ev.key, ev.released ? 'r' : 'p');
      diag_loop_add(d);
    }
  }
}

// ---- 車機顯示曲名：非阻塞湊一行 Serial2（S3→WROOM），交給 np_parse。----
static char g_npLineBuf[512];
static size_t g_npLineLen = 0;
static bool g_npLineOverflow = false;

static void pollNowPlayingFromS3() {
  while (Serial2.available() > 0) {
    char c = (char)Serial2.read();
    if (c == '\n') {
      if (!g_npLineOverflow && g_npLineLen > 0) {
        NowPlaying np;
        if (np_parse(g_npLineBuf, g_npLineLen, np)) {
          np.rx_ms = millis();
          bool firstTime = !g_np.valid;
          bool trackChanged = firstTime || !np_same_track(g_np, np);
          g_np = np;
          avrc_meta_set(np);
          ds_inc(g_sum.meta_lines);
          g_sum_dirty = true;
          if (firstTime) diag_loop_add("M ok");
          if (firstTime || trackChanged) {
            Serial.printf("[Meta] %s / %s\n", np.title, np.artist);
          }
          if (trackChanged) {
            g_rn_track_due = true;
            g_rn_pos_force_due = true;
          }
          // S3 每次設曲目都等效「現在在播放」；第一筆曲目進來也要送一次 PLAY_STATUS
          // CHANGED（修掉第一筆不通知的 bug），車機才會真的去問 GetElementAttributes。
          if (firstTime) g_rn_status_due = true;
        }
      }
      g_npLineLen = 0;
      g_npLineOverflow = false;
      continue;
    }
    if (c == '\r') continue;
    if (g_npLineLen >= sizeof(g_npLineBuf) - 1) {
      g_npLineOverflow = true;  // 超長丟棄整行
      continue;
    }
    g_npLineBuf[g_npLineLen++] = c;
  }
}

// loop() 呼叫：已註冊的通知若有變化就送 CHANGED，然後清旗標——AVRCP 規定每次註冊只能
// 回一次 CHANGED，車機收到後會再重新註冊。
static void sendDueRnNotifications() {
  if (g_rn_track_registered && g_rn_track_due) {
    esp_avrc_rn_param_t param;
    memset(&param, 0, sizeof(param));
    np_fill_elm_id(g_np, param.elm_id);
    esp_avrc_tg_send_rn_rsp(ESP_AVRC_RN_TRACK_CHANGE, ESP_AVRC_RN_RSP_CHANGED, &param);
    g_rn_track_registered = false;
    g_rn_track_due = false;
  }
  if (g_rn_status_registered && g_rn_status_due) {
    esp_avrc_rn_param_t param;
    memset(&param, 0, sizeof(param));
    param.playback = (esp_avrc_playback_stat_t)g_reported_playback;
    esp_avrc_tg_send_rn_rsp(ESP_AVRC_RN_PLAY_STATUS_CHANGE, ESP_AVRC_RN_RSP_CHANGED, &param);
    g_rn_status_registered = false;
    g_rn_status_due = false;
  }
  uint32_t nowMs = millis();
  bool intervalDue = g_reported_playback == ESP_AVRC_PLAYBACK_PLAYING && g_np.state == 'P' &&
                      (nowMs - g_rn_pos_last_sent_ms) >= g_rn_pos_interval_s * 1000;
  if (g_rn_pos_registered && (intervalDue || g_rn_pos_force_due)) {
    esp_avrc_rn_param_t param;
    memset(&param, 0, sizeof(param));
    param.play_pos = np_position_now(g_np, nowMs);
    esp_avrc_tg_send_rn_rsp(ESP_AVRC_RN_PLAY_POS_CHANGED, ESP_AVRC_RN_RSP_CHANGED, &param);
    g_rn_pos_registered = false;
    g_rn_pos_force_due = false;
    g_rn_pos_last_sent_ms = nowMs;
  }
}

// DIAG：第一次 GetElementAttributes/GetPlayStatus 命中各記一筆（每次開機各一次），用來
// 在車上證明車機真的有來問。
static bool g_diag_elem_logged = false;
static bool g_diag_status_logged = false;
static bool g_diag_unserved_elem_logged = false;
static bool g_diag_unserved_status_logged = false;
static void diagLogFirstAvrcHits() {
  if (!g_diag_elem_logged && avrc_meta_hits_elem() > 0) {
    g_diag_elem_logged = true;
    diag_loop_add("G elem");
  }
  if (!g_diag_status_logged && avrc_meta_hits_status() > 0) {
    g_diag_status_logged = true;
    diag_loop_add("G stat");
  }
  if (!g_diag_unserved_elem_logged && avrc_meta_unserved_elem() > 0) {
    g_diag_unserved_elem_logged = true;
    diag_loop_add("G nov e");
  }
  if (!g_diag_unserved_status_logged && avrc_meta_unserved_status() > 0) {
    g_diag_unserved_status_logged = true;
    diag_loop_add("G nov s");
  }

  uint32_t raw_elem = avrc_meta_hits_elem();
  uint32_t raw_stat = avrc_meta_hits_status();
  uint32_t raw_nov_e = avrc_meta_unserved_elem();
  uint32_t raw_nov_s = avrc_meta_unserved_status();
  uint16_t elem = (uint16_t)(raw_elem > 0xFFFF ? 0xFFFF : raw_elem);
  uint16_t stat = (uint16_t)(raw_stat > 0xFFFF ? 0xFFFF : raw_stat);
  uint16_t nov_e = (uint16_t)(raw_nov_e > 0xFFFF ? 0xFFFF : raw_nov_e);
  uint16_t nov_s = (uint16_t)(raw_nov_s > 0xFFFF ? 0xFFFF : raw_nov_s);
  if (elem != g_sum.g_elem || stat != g_sum.g_stat || nov_e != g_sum.nov_elem || nov_s != g_sum.nov_stat) {
    g_sum.g_elem = elem;
    g_sum.g_stat = stat;
    g_sum.nov_elem = nov_e;
    g_sum.nov_stat = nov_s;
    g_sum_dirty = true;
  }
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
  if (state == ESP_A2D_CONNECTION_STATE_DISCONNECTED && prev == ESP_A2D_CONNECTION_STATE_CONNECTED) {
    g_dropped_evt = true;
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

  // 車機顯示曲名/方向盤按鍵的 UART，接線見 car_puck.ino PIN_WROOM_UART_* 定義前註解。
  Serial2.begin(115200, SERIAL_8N1, 16, 17);

  a2dp_source.set_data_callback_in_frames(bridge_data_cb);
  a2dp_source.set_ssid_callback(ssid_match_cb);
  a2dp_source.set_on_connection_state_changed(connection_state_cb);
  a2dp_source.set_auto_reconnect(true);
  // 開 SSP：IO 能力宣告為 DisplayYesNo（ESP_BT_IO_CAP_IO），比對數字請求函式庫自動同意。
  // 預設 false 不設 IO 能力，BMW 回 AUTH_CMPL stat=9（AUTH_FAILURE，10/7 [DIAG] 實測）。
  a2dp_source.set_ssp_enabled(true);
  a2dp_source.set_volume(127);
  // 音量交給車機：函式庫預設音量曲線會把 PCM 一起衰減，車機再衰減一次（見 BridgeSource::set_volume）。
  a2dp_source.set_volume_control(&g_no_vol);
  // 啟用 AVRCP Target（函式庫內部才會 esp_avrc_tg_init()，現在的韌體沒呼叫，TG 根本沒起來）：
  // 車機顯示曲名需要回應 GetElementAttributes/GetPlayStatus；方向盤按鍵走 passthrough。
  a2dp_source.set_avrc_passthru_command_callback(on_wheel_key);
  a2dp_source.set_avrc_rn_events({ESP_AVRC_RN_VOLUME_CHANGE, ESP_AVRC_RN_PLAY_STATUS_CHANGE,
                                   ESP_AVRC_RN_TRACK_CHANGE, ESP_AVRC_RN_PLAY_POS_CHANGED});
  a2dp_source.start();
  // 已配對清單在 start() 之後才讀：start() 內部已 init NVS，我們這邊再 init 會讓函式庫 ESP_ERROR_CHECK 失敗
  known_load();
  diag_load();
  sum_load();
  g_sum = DiagSummary();
  g_pairing_mode = a2dp_source.pairing_mode();
  Serial.printf("[BT] 模式：%s\n", g_pairing_mode ? "配對(掃描)" : "重連已知裝置");
  if (!g_pairing_mode) {
    uint8_t lc[6];
    a2dp_source.get_last(lc);
    char d[DIAG_TEXT];
    snprintf(d, sizeof(d), "B r %02X%02X%02X k%d", lc[3], lc[4], lc[5], (int)g_known.count);
    recon_diag_add(d);
  }
}

void loop() {
  static uint32_t last_ms = 0;
  static uint32_t last_frames = 0;
  static uint32_t led_ms = 0;
  static bool led_on = false;
  static bool boot_was_down = false;
  static uint32_t boot_down_ms = 0;

  diag_drain();
  pollSerialCommand();
  pollNowPlayingFromS3();
  sendDueRnNotifications();
  drainWheelKeys();
  diagLogFirstAvrcHits();

  // 統計摘要存 NVS：有變化且距上次存 >=3s，或每 60s 定期存一次（更新 uptime）。
  {
    uint32_t nowMs = millis();
    bool dueDirty = g_sum_dirty && (nowMs - g_sum_last_store_ms >= 3000);
    bool duePeriodic = (nowMs - g_sum_last_store_ms >= 60000);
    if (dueDirty || duePeriodic) {
      g_sum.uptime_s = nowMs / 1000;
      sum_store();
      g_sum_dirty = false;
      g_sum_last_store_ms = nowMs;
    }
  }

  if (g_connected_evt) {
    g_connected_evt = false;
    if (a2dp_source.arm_reconnect()) {
      Serial.println("[BT] 首次配對：補開自動重連");
    }
    uint8_t cur[6];
    if (g_conn_bda_valid) {
      memcpy(cur, g_conn_bda, 6);
    } else {
      a2dp_source.get_last(cur);
    }
    char conn_from = g_conn_from;
    g_conn_bda_valid = false;
    g_conn_from = '?';
    Serial.printf("[BT] 連上來源=%c\n", conn_from);
    g_sum.peer[0] = cur[3];
    g_sum.peer[1] = cur[4];
    g_sum.peer[2] = cur[5];
    g_sum_dirty = true;
    if (!addr_is_zero(cur)) {
      known_touch(g_known, cur);
      known_save();
      Serial.print("[BT] 已記住裝置 ");
      print_bda(cur);
      Serial.printf("（共 %d 台）\n", (int)g_known.count);
    }
    // C/F/D 重連模式下也記（每次開機上限 RECON_DIAG_MAX 筆，避免目標沒開機每 ~10s 失敗一次整晚寫 NVS）
    {
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "C %02X%02X%02X %c", cur[3], cur[4], cur[5], conn_from);
      if (g_pairing_mode) {
        diag_loop_add(d);
      } else {
        recon_diag_add(d);
      }
    }
    g_pairing_mode = false;
  }

  if (g_page_failed_evt) {
    g_page_failed_evt = false;
    {
      uint8_t lc[6];
      a2dp_source.get_last(lc);
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "F %02X%02X%02X", lc[3], lc[4], lc[5]);
      if (g_pairing_mode) {
        diag_loop_add(d);
      } else {
        recon_diag_add(d);
      }
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

  if (g_dropped_evt) {
    g_dropped_evt = false;
    uint8_t lc[6];
    a2dp_source.get_last(lc);
    char d[DIAG_TEXT];
    snprintf(d, sizeof(d), "D %02X%02X%02X", lc[3], lc[4], lc[5]);
    recon_diag_add(d);
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
