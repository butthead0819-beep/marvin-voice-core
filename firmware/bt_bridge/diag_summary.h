// diag_summary.h — bt_bridge 每次開機統計摘要（純 C++，不依賴 Arduino / ESP-IDF，可在 host 端測試）
//
// 一趟車 6 分鐘，diag_log.h 的環形只留 16 筆，會被重連失敗/H u 等事件擠掉。這裡改成固定
// 一筆統計摘要，隨時覆寫，不會被擠掉；並保留上一次開機的摘要（見 bt_bridge.ino 的
// "sum0"/"sum1" NVS key）。
#ifndef DIAG_SUMMARY_H
#define DIAG_SUMMARY_H

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

struct DiagSummary {
  uint32_t uptime_s = 0;   // 最後一次更新時的開機秒數
  uint16_t meta_lines = 0; // 從 S3 解析成功的曲目行數（飽和在 65535）
  uint16_t g_elem = 0, g_stat = 0;        // 有資料、成功回應 GetElementAttributes / GetPlayStatus 次數
  uint16_t nov_elem = 0, nov_stat = 0;    // 車機有問但沒資料（放行拒絕）次數
  uint16_t rn_mask = 0;    // 車機註冊過的 AVRCP 通知 event_id bitmask（0–15）
  uint32_t rf_feat = 0;    // 車機 CT feat_mask（REMOTE_FEATURES_EVT，最後一次）
  uint16_t rf_ct = 0;      // ct_feat_flag
  uint16_t bvra_on = 0, bvra_off = 0;     // HFP BVRA value=1 / value=0 次數
  uint16_t sco_open = 0, sco_timeout = 0; // SCO 連上次數（AUDIO_STATE CONNECTED 或 CONNECTED_MSBC）/ 安全逾時次數
  uint8_t  hf_state = 0;   // 最後一次 HFP 連線狀態
  uint8_t  key_n = 0;      // keys[] 已用數
  uint8_t  keys[8] = {0};  // 收過的 passthrough key code（去重，最多 8 種）
  uint8_t  peer[3] = {0};  // 最後一次 A2DP 連上的位址後 3 bytes
};

// 飽和 +1（不溢位到 0）。
inline void ds_inc(uint16_t& c) {
  if (c < 0xFFFF) c++;
}

// 去重加入；滿了（已有 8 種）忽略。
inline void ds_add_key(DiagSummary& s, uint8_t k) {
  for (int i = 0; i < s.key_n; i++) {
    if (s.keys[i] == k) return;
  }
  if (s.key_n >= 8) return;
  s.keys[s.key_n++] = k;
}

// 格式化成一行（不含換行），寫進 out（cap 至少 160）。cap 不夠就截斷，保證 '\0' 結尾。
inline const char* ds_format(const DiagSummary& s, char* out, size_t cap) {
  char keybuf[32] = {0};
  size_t keylen = 0;
  for (int i = 0; i < s.key_n && keylen + 3 < sizeof(keybuf); i++) {
    int n = snprintf(keybuf + keylen, sizeof(keybuf) - keylen, "%s%02x", i > 0 ? "," : "", (unsigned)s.keys[i]);
    if (n < 0) break;
    keylen += (size_t)n;
  }
  snprintf(out, cap, "t=%lus m=%u ge=%u gs=%u ne=%u ns=%u rn=%04x rf=%lx/%x bv=%u/%u sco=%u/%u hf=%u k=%s p=%02X%02X%02X",
           (unsigned long)s.uptime_s, (unsigned)s.meta_lines, (unsigned)s.g_elem, (unsigned)s.g_stat,
           (unsigned)s.nov_elem, (unsigned)s.nov_stat, (unsigned)s.rn_mask, (unsigned long)s.rf_feat,
           (unsigned)s.rf_ct, (unsigned)s.bvra_on, (unsigned)s.bvra_off, (unsigned)s.sco_open,
           (unsigned)s.sco_timeout, (unsigned)s.hf_state, keybuf, s.peer[0], s.peer[1], s.peer[2]);
  return out;
}

#endif  // DIAG_SUMMARY_H
