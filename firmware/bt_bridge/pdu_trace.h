// pdu_trace.h — AVRCP metadata PDU 追蹤（純 C++，不依賴 Arduino / ESP-IDF，可在 host 端測試）
//
// 只記錄「WROOM 對車機哪些 AVRCP metadata PDU 回了什麼狀態碼」，不改任何行為。用來分辨
// 車機完全沒送 metadata 指令，還是送了但被我們的回應勸退。見 bt_bridge.ino 檔頭
// "AVRCP 曲名診斷 DIAG 代碼" 的 "T ..." 說明。
#ifndef PDU_TRACE_H
#define PDU_TRACE_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

struct PduEntry {
  char src = 0;      // 'B' = 經 AVRC_BldResponse；'V' = 經 AVRC_VendorRsp
  uint8_t pdu = 0;
  uint8_t st = 0;    // 最新一次的狀態碼（B：AVRC status；V：ctype）
  uint16_t cnt = 0;  // 飽和在 0xFFFF
};

struct PduTrace {
  PduEntry e[8];
  uint8_t n = 0;
  bool caps_seen = false;
  uint8_t caps_n = 0;        // GetCapabilities(EVENTS_SUPPORTED) 回應的原始 count（不截）
  uint8_t caps_ev[8] = {0};  // 前 min(count, 8) 個 event id
};

// 以 (src,pdu) 為鍵：已存在 → 更新 st 為最新值、cnt 飽和 +1，回 false；不存在且未滿 8 →
// 新增（cnt=1），回 true；滿了 → 忽略，回 false。
inline bool pt_record(PduTrace& t, char src, uint8_t pdu, uint8_t st) {
  for (uint8_t i = 0; i < t.n; i++) {
    if (t.e[i].src == src && t.e[i].pdu == pdu) {
      t.e[i].st = st;
      if (t.e[i].cnt < 0xFFFF) t.e[i].cnt++;
      return false;
    }
  }
  if (t.n >= 8) return false;
  t.e[t.n].src = src;
  t.e[t.n].pdu = pdu;
  t.e[t.n].st = st;
  t.e[t.n].cnt = 1;
  t.n++;
  return true;
}

// GetCapabilities(EVENTS_SUPPORTED) 回應內容：caps_n 存原始 count（不截），caps_ev 存前
// min(count,8) 個 event id。可重複呼叫，以最新一次為準。ev 可為 nullptr（count==0 時）。
inline void pt_set_caps(PduTrace& t, uint8_t count, const uint8_t* ev) {
  t.caps_seen = true;
  t.caps_n = count;
  uint8_t take = count < 8 ? count : 8;
  for (uint8_t i = 0; i < take; i++) t.caps_ev[i] = ev[i];
  for (uint8_t i = take; i < 8; i++) t.caps_ev[i] = 0;
}

// "T B10 s04"：src 字元、pdu 兩位小寫 hex、s+st 兩位小寫 hex。保證 '\0' 結尾，cap 不夠就截斷。
inline const char* pt_format_entry(const PduEntry& e, char* out, size_t cap) {
  snprintf(out, cap, "T %c%02x s%02x", e.src, (unsigned)e.pdu, (unsigned)e.st);
  return out;
}

// "T caps n4 0d,01,02,05"：n 後是 caps_n 十進位，接最多 8 個 event id 兩位小寫 hex 逗號分隔；
// count==0 時是 "T caps n0"。保證 '\0' 結尾，cap 不夠就截斷。
inline const char* pt_format_caps(const PduTrace& t, char* out, size_t cap) {
  char idbuf[32] = {0};
  size_t idlen = 0;
  uint8_t take = t.caps_n < 8 ? t.caps_n : 8;
  for (uint8_t i = 0; i < take && idlen + 3 < sizeof(idbuf); i++) {
    int n = snprintf(idbuf + idlen, sizeof(idbuf) - idlen, "%s%02x", i > 0 ? "," : "", (unsigned)t.caps_ev[i]);
    if (n < 0) break;
    idlen += (size_t)n;
  }
  if (t.caps_n == 0) {
    snprintf(out, cap, "T caps n0");
  } else {
    snprintf(out, cap, "T caps n%u %s", (unsigned)t.caps_n, idbuf);
  }
  return out;
}

#endif  // PDU_TRACE_H
