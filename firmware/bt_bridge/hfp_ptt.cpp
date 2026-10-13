// hfp_ptt.cpp — 見 hfp_ptt.h。
//
// hfp_cb() 跑在 BTC task：只做協定回應＋推事件進佇列，不准 Serial.print、不准寫 NVS。
// 事件佇列是單生產者（BTC task）單消費者（loop）ring，仿 bt_bridge.ino 方向盤 ring 的寫法：
// head 只被 producer 動、tail 只被 consumer 動，滿了丟。

#include "hfp_ptt.h"

#include <Arduino.h>
#include <string.h>

#include "esp_hf_ag_api.h"
#include "diag_log.h"

// bt_bridge.ino 內的 diag_loop_add() 把文字加進 g_diag 並存 NVS；移除其 static 以便這裡呼叫。
extern void diag_loop_add(const char* text);

namespace {

struct HfpEvent {
  char kind;
  int32_t val;
};

const int HFP_Q = 32;
HfpEvent s_q[HFP_Q];
volatile uint8_t s_q_head = 0;
volatile uint8_t s_q_tail = 0;

void q_push(char kind, int32_t val) {
  uint8_t next = (uint8_t)((s_q_head + 1) % HFP_Q);
  if (next == s_q_tail) return;  // 滿了，丟
  s_q[s_q_head] = {kind, val};
  s_q_head = next;
}

uint8_t s_peer[6] = {0, 0, 0, 0, 0, 0};
volatile bool s_peer_valid = false;

volatile esp_hf_connection_state_t s_hf_conn_state = ESP_HF_CONNECTION_STATE_DISCONNECTED;
volatile esp_hf_audio_state_t s_audio_state = ESP_HF_AUDIO_STATE_DISCONNECTED;
volatile uint32_t s_sco_open_ms = 0;  // 0 = 未開
volatile uint32_t s_sco_rx_frames = 0;

const uint8_t HFP_DIAG_MAX = 16;
uint8_t s_diag_used = 0;

const uint32_t SCO_SAFETY_TIMEOUT_MS = 15000;
const uint32_t SCO_RX_LOG_INTERVAL_MS = 5000;

void sco_rx_cb(esp_hf_sync_conn_hdl_t sync_conn_hdl, esp_hf_audio_buff_t* audio_buf, bool is_bad_frame) {
  (void)sync_conn_hdl;
  (void)is_bad_frame;
  s_sco_rx_frames++;
  esp_hf_ag_audio_buff_free(audio_buf);
}

void hfp_cb(esp_hf_cb_event_t event, esp_hf_cb_param_t* param) {
  switch (event) {
    case ESP_HF_CONNECTION_STATE_EVT: {
      s_hf_conn_state = param->conn_stat.state;
      if (param->conn_stat.state == ESP_HF_CONNECTION_STATE_SLC_CONNECTED && !s_peer_valid) {
        memcpy(s_peer, param->conn_stat.remote_bda, 6);
        s_peer_valid = true;
      }
      q_push('c', (int32_t)param->conn_stat.state);
      break;
    }

    case ESP_HF_AUDIO_STATE_EVT: {
      s_audio_state = param->audio_stat.state;
      if (param->audio_stat.state == ESP_HF_AUDIO_STATE_DISCONNECTED) {
        s_sco_open_ms = 0;
      }
      q_push('a', (int32_t)param->audio_stat.state);
      break;
    }

    case ESP_HF_BVRA_RESPONSE_EVT: {
      q_push('v', (int32_t)param->vra_rep.value);
      if (param->vra_rep.value == ESP_HF_VR_STATE_ENABLED) {
        esp_hf_ag_audio_connect(param->vra_rep.remote_addr);
        s_sco_open_ms = millis();
      } else {
        esp_hf_ag_audio_disconnect(param->vra_rep.remote_addr);
      }
      break;
    }

    case ESP_HF_CIND_RESPONSE_EVT: {
      esp_hf_ag_cind_response(param->cind_rep.remote_addr,
                               ESP_HF_CALL_STATUS_NO_CALLS,
                               ESP_HF_CALL_SETUP_STATUS_IDLE,
                               ESP_HF_NETWORK_STATE_AVAILABLE,
                               4,
                               ESP_HF_ROAMING_STATUS_INACTIVE,
                               5,
                               ESP_HF_CALL_HELD_STATUS_NONE);
      break;
    }

    case ESP_HF_COPS_RESPONSE_EVT: {
      esp_hf_ag_cops_response(param->cops_rep.remote_addr, (char*)"Marvin");
      break;
    }

    case ESP_HF_CLCC_RESPONSE_EVT: {
      // 沒有通話：只送終止回應（index 0），讓車機知道清單是空的。
      esp_hf_ag_clcc_response(param->clcc_rep.remote_addr,
                               0,
                               ESP_HF_CURRENT_CALL_DIRECTION_OUTGOING,
                               ESP_HF_CURRENT_CALL_STATUS_ACTIVE,
                               ESP_HF_CURRENT_CALL_MODE_VOICE,
                               ESP_HF_CURRENT_CALL_MPTY_TYPE_SINGLE,
                               NULL,
                               ESP_HF_CALL_ADDR_TYPE_UNKNOWN);
      break;
    }

    case ESP_HF_CNUM_RESPONSE_EVT: {
      esp_hf_ag_cnum_response(param->cnum_rep.remote_addr, (char*)"0", 129, ESP_HF_SUBSCRIBER_SERVICE_TYPE_VOICE);
      break;
    }

    case ESP_HF_UNAT_RESPONSE_EVT: {
      esp_hf_ag_unknown_at_send(param->unat_rep.remote_addr, NULL);
      q_push('u', 0);
      break;
    }

    case ESP_HF_DIAL_EVT: {
      esp_hf_ag_cmee_send(param->out_call.remote_addr, ESP_HF_AT_RESPONSE_CODE_ERR, ESP_HF_CME_AG_FAILURE);
      q_push('d', 0);
      break;
    }

    case ESP_HF_ATA_RESPONSE_EVT: {
      esp_hf_ag_cmee_send(param->ata_rep.remote_addr, ESP_HF_AT_RESPONSE_CODE_ERR, ESP_HF_CME_AG_FAILURE);
      q_push('A', 0);
      break;
    }

    case ESP_HF_CHUP_RESPONSE_EVT: {
      esp_hf_ag_cmee_send(param->chup_rep.remote_addr, ESP_HF_AT_RESPONSE_CODE_ERR, ESP_HF_CME_AG_FAILURE);
      q_push('h', 0);
      break;
    }

    case ESP_HF_VOLUME_CONTROL_EVT: {
      q_push('g', (int32_t)param->volume_control.type * 1000 + param->volume_control.volume);
      break;
    }

    default: {
      q_push('e', (int32_t)event);
      break;
    }
  }
}

}  // namespace

void hfp_ptt_init() {
  esp_err_t err = esp_hf_ag_register_callback(hfp_cb);
  if (err != ESP_OK) Serial.printf("[HFP] register_callback 失敗 err=%d\n", (int)err);

  err = esp_hf_ag_init();
  if (err != ESP_OK) Serial.printf("[HFP] init 失敗 err=%d\n", (int)err);

  err = esp_hf_ag_register_audio_data_callback(sco_rx_cb);
  if (err != ESP_OK) Serial.printf("[HFP] register_audio_data_callback 失敗 err=%d\n", (int)err);
}

void hfp_ptt_on_a2dp_connected(const uint8_t bda[6]) {
  memcpy(s_peer, bda, 6);
  s_peer_valid = true;
  if (s_hf_conn_state == ESP_HF_CONNECTION_STATE_DISCONNECTED) {
    esp_err_t err = esp_hf_ag_slc_connect(s_peer);
    Serial.printf("[HFP] slc_connect err=%d\n", (int)err);
  }
}

void hfp_ptt_poll() {
  while (true) {
    uint8_t tail = s_q_tail;
    if (tail == s_q_head) break;
    HfpEvent ev = s_q[tail];
    s_q_tail = (uint8_t)((tail + 1) % HFP_Q);

    Serial.printf("[HFP] %c %ld\n", ev.kind, (long)ev.val);
    if (diag_budget_take(s_diag_used, HFP_DIAG_MAX)) {
      char d[DIAG_TEXT];
      snprintf(d, sizeof(d), "H %c %ld", ev.kind, (long)ev.val);
      diag_loop_add(d);
    }
    if (ev.kind == 'v') {
      Serial2.printf("P|%ld\n", (long)ev.val);
    }
  }

  if (s_sco_open_ms != 0 && (millis() - s_sco_open_ms) > SCO_SAFETY_TIMEOUT_MS) {
    esp_hf_ag_vra_control(s_peer, ESP_HF_VR_STATE_DISABLED);
    esp_hf_ag_audio_disconnect(s_peer);
    s_sco_open_ms = 0;
    if (diag_budget_take(s_diag_used, HFP_DIAG_MAX)) diag_loop_add("H T");
  }

  static uint32_t s_last_sco_log_ms = 0;
  uint32_t now = millis();
  bool sco_open = (s_audio_state == ESP_HF_AUDIO_STATE_CONNECTED || s_audio_state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC);
  if (sco_open && now - s_last_sco_log_ms >= SCO_RX_LOG_INTERVAL_MS) {
    s_last_sco_log_ms = now;
    Serial.printf("[HFP] sco_rx=%lu\n", (unsigned long)s_sco_rx_frames);
  }
}
